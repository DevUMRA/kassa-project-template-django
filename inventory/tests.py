import io
import itertools
import shutil
import tempfile
import threading
from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock, skipUnless

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from . import backup
from .models import Document, DocumentItem, Product, Stock, StockMovement, Supplier, User, Warehouse
from .services import cancel_document, post_document, start_inventory

T = Document.Type
S = Document.Status

_doc_numbers = itertools.count(1)


def create_fixtures(target):
    target.manager = User.objects.create_user('manager', role=User.Role.MANAGER)
    target.warehouseman = User.objects.create_user('warehouseman', role=User.Role.WAREHOUSEMAN)
    target.seller = User.objects.create_user('seller', role=User.Role.SELLER)
    target.warehouse = Warehouse.objects.create(name='Басты қойма')
    target.other_warehouse = Warehouse.objects.create(name='Екінші қойма')
    target.supplier = Supplier.objects.create(name='Серіктес ТОО')
    target.other_supplier = Supplier.objects.create(name='Басқа жеткізуші')
    target.product = Product.objects.create(
        name='Су 0.5л', sku='WATER-05', barcode='4870001112233', sale_price=Decimal('150')
    )
    target.other_product = Product.objects.create(name='Шырын 1л', sku='JUICE-1', sale_price=Decimal('500'))


class ServiceMixin:
    def make_doc(self, doc_type, qty, user=None, product=None, warehouse=None,
                 supplier=None, base=None, price=Decimal('100'), comment=None):
        if comment is None:
            # Есептен шығаруда себеп міндетті
            comment = 'Бүлінген' if doc_type == T.WRITE_OFF else ''
        document = Document.objects.create(
            number=f'T-{next(_doc_numbers)}',
            doc_type=doc_type,
            warehouse=warehouse or self.warehouse,
            supplier=supplier,
            responsible=user or self.manager,
            base_document=base,
            comment=comment,
        )
        DocumentItem.objects.create(
            document=document, product=product or self.product, quantity=Decimal(qty), price=price
        )
        return document

    def receive(self, qty, **kwargs):
        kwargs.setdefault('supplier', self.supplier)
        return post_document(self.make_doc(T.RECEIPT, qty, **kwargs), self.warehouseman)

    def stock(self, product=None, warehouse=None):
        return Stock.objects.get(product=product or self.product, warehouse=warehouse or self.warehouse).quantity


class ServiceTestCase(ServiceMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        create_fixtures(cls)


class PostDocumentTests(ServiceTestCase):
    def test_receipt_increases_stock_and_writes_movement(self):
        document = self.receive(100)

        self.assertEqual(document.status, S.POSTED)
        self.assertIsNotNone(document.posted_at)
        self.assertEqual(self.stock(), 100)
        movement = StockMovement.objects.get(document=document)
        self.assertEqual(movement.quantity, 100)
        self.assertEqual(movement.doc_type, T.RECEIPT)
        self.assertEqual(movement.user, self.warehouseman)
        self.assertFalse(movement.is_reversal)

    def test_sale_decreases_stock(self):
        self.receive(100)
        post_document(self.make_doc(T.SALE, 10), self.seller)

        self.assertEqual(self.stock(), 90)

    def test_write_off_decreases_stock(self):
        self.receive(100)
        post_document(self.make_doc(T.WRITE_OFF, 2), self.warehouseman)

        self.assertEqual(self.stock(), 98)

    def test_repost_is_rejected(self):
        document = self.receive(100)

        with self.assertRaises(ValidationError):
            post_document(document, self.warehouseman)
        self.assertEqual(self.stock(), 100)
        self.assertEqual(StockMovement.objects.filter(document=document).count(), 1)

    def test_insufficient_stock_is_rejected(self):
        self.receive(10)
        sale = self.make_doc(T.SALE, 15)

        with self.assertRaisesMessage(ValidationError, 'Қалдық жеткіліксіз'):
            post_document(sale, self.seller)
        sale.refresh_from_db()
        self.assertEqual(sale.status, S.DRAFT)
        self.assertEqual(self.stock(), 10)

    def test_sale_without_any_stock_is_rejected(self):
        with self.assertRaisesMessage(ValidationError, 'Қалдық жеткіліксіз'):
            post_document(self.make_doc(T.SALE, 1), self.seller)

    def test_empty_document_is_rejected(self):
        document = Document.objects.create(
            number='EMPTY', doc_type=T.RECEIPT, warehouse=self.warehouse,
            supplier=self.supplier, responsible=self.manager,
        )
        with self.assertRaisesMessage(ValidationError, 'бірде-бір тауар жоқ'):
            post_document(document, self.manager)

    def test_receipt_requires_supplier(self):
        with self.assertRaisesMessage(ValidationError, 'жеткізуші көрсетілуі тиіс'):
            post_document(self.make_doc(T.RECEIPT, 10), self.manager)

    def test_inactive_product_and_warehouse_are_rejected(self):
        self.product.is_active = False
        self.product.save()
        self.warehouse.is_active = False
        self.warehouse.save()

        with self.assertRaises(ValidationError) as ctx:
            post_document(self.make_doc(T.RECEIPT, 10, supplier=self.supplier), self.manager)
        self.assertEqual(len(ctx.exception.messages), 2)

    def test_zero_quantity_is_rejected(self):
        with self.assertRaisesMessage(ValidationError, 'нөлден үлкен'):
            post_document(self.make_doc(T.RECEIPT, 0, supplier=self.supplier), self.manager)


class RolePermissionTests(ServiceTestCase):
    def test_seller_cannot_post_receipt(self):
        with self.assertRaises(PermissionDenied):
            post_document(self.make_doc(T.RECEIPT, 10, supplier=self.supplier), self.seller)

    def test_warehouseman_cannot_post_sale(self):
        self.receive(10)
        with self.assertRaises(PermissionDenied):
            post_document(self.make_doc(T.SALE, 1), self.warehouseman)

    def test_only_manager_approves_inventory(self):
        for user in (self.seller, self.warehouseman):
            with self.assertRaises(PermissionDenied):
                post_document(self.make_doc(T.INVENTORY, 5), user)

    def test_superuser_can_post_anything(self):
        admin = User.objects.create_superuser('admin', role=User.Role.SELLER)
        post_document(self.make_doc(T.RECEIPT, 10, supplier=self.supplier), admin)

        self.assertEqual(self.stock(), 10)


class InventoryTests(ServiceTestCase):
    def test_inventory_sets_stock_to_counted_quantity(self):
        self.receive(100)
        document = post_document(self.make_doc(T.INVENTORY, 97), self.manager)

        self.assertEqual(self.stock(), 97)
        self.assertEqual(StockMovement.objects.get(document=document).quantity, -3)

    def test_inventory_without_difference_writes_no_movement(self):
        self.receive(100)
        document = post_document(self.make_doc(T.INVENTORY, 100), self.manager)

        self.assertEqual(self.stock(), 100)
        self.assertFalse(StockMovement.objects.filter(document=document).exists())

    def test_draft_inventory_blocks_movements_of_its_products(self):
        self.receive(100)
        self.make_doc(T.INVENTORY, 97)

        with self.assertRaisesMessage(ValidationError, 'түгендеуінде бұғатталған'):
            post_document(self.make_doc(T.SALE, 1), self.seller)

    def test_draft_inventory_does_not_block_other_products_or_warehouses(self):
        self.receive(10)
        self.receive(10, product=self.other_product)
        self.receive(10, warehouse=self.other_warehouse)
        self.make_doc(T.INVENTORY, 5)

        post_document(self.make_doc(T.SALE, 1, product=self.other_product), self.seller)
        post_document(self.make_doc(T.SALE, 1, warehouse=self.other_warehouse), self.seller)

        self.assertEqual(self.stock(self.other_product), 9)
        self.assertEqual(self.stock(warehouse=self.other_warehouse), 9)


class CancelDocumentTests(ServiceTestCase):
    def test_cancel_posted_writes_reversal_and_restores_stock(self):
        self.receive(100)
        sale = post_document(self.make_doc(T.SALE, 10), self.seller)

        cancel_document(sale, self.manager, 'Қате енгізілген')

        sale.refresh_from_db()
        self.assertEqual(sale.status, S.CANCELED)
        self.assertIsNotNone(sale.canceled_at)
        self.assertEqual(self.stock(), 100)
        quantities = list(sale.movements.order_by('id').values_list('quantity', 'is_reversal'))
        self.assertEqual(quantities, [(Decimal(-10), False), (Decimal(10), True)])

    def test_cancel_draft_changes_only_status(self):
        sale = self.make_doc(T.SALE, 10, user=self.seller)

        cancel_document(sale, self.seller)

        sale.refresh_from_db()
        self.assertEqual(sale.status, S.CANCELED)
        self.assertFalse(sale.movements.exists())

    def test_cancel_twice_is_rejected(self):
        self.receive(100)
        sale = post_document(self.make_doc(T.SALE, 10), self.seller)
        cancel_document(sale, self.manager, 'Қате енгізілген')

        with self.assertRaises(ValidationError):
            cancel_document(sale, self.manager, 'Қате енгізілген')
        self.assertEqual(self.stock(), 100)

    def test_only_manager_cancels_posted(self):
        self.receive(100)
        sale = post_document(self.make_doc(T.SALE, 10), self.seller)

        with self.assertRaises(PermissionDenied):
            cancel_document(sale, self.seller)

    def test_cancel_that_would_make_stock_negative_is_rejected(self):
        receipt = self.receive(100)
        post_document(self.make_doc(T.SALE, 10), self.seller)

        with self.assertRaisesMessage(ValidationError, 'Қалдық жеткіліксіз'):
            cancel_document(receipt, self.manager, 'Қате енгізілген')
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, S.POSTED)
        self.assertEqual(self.stock(), 90)


class SaleReturnTests(ServiceTestCase):
    def setUp(self):
        self.receive(100)
        self.sale = post_document(self.make_doc(T.SALE, 5), self.seller)

    def sale_return(self, qty, base=None, **kwargs):
        return self.make_doc(T.SALE_RETURN, qty, base=base, **kwargs)

    def test_return_within_sold_quantity(self):
        post_document(self.sale_return(2, self.sale), self.seller)
        post_document(self.sale_return(3, self.sale), self.seller)

        self.assertEqual(self.stock(), 100)

    def test_return_requires_base_sale(self):
        with self.assertRaisesMessage(ValidationError, 'негіз құжат'):
            post_document(self.sale_return(1), self.seller)

    def test_return_more_than_sold_is_rejected(self):
        post_document(self.sale_return(2, self.sale), self.seller)

        with self.assertRaisesMessage(ValidationError, 'қайтаруға болатыны 3.000'):
            post_document(self.sale_return(4, self.sale), self.seller)
        self.assertEqual(self.stock(), 97)

    def test_canceled_returns_free_up_quantity(self):
        first = post_document(self.sale_return(5, self.sale), self.seller)
        cancel_document(first, self.manager, 'Қате енгізілген')

        post_document(self.sale_return(5, self.sale), self.seller)

        self.assertEqual(self.stock(), 100)

    def test_return_from_canceled_sale_is_rejected(self):
        cancel_document(self.sale, self.manager, 'Қате енгізілген')

        with self.assertRaisesMessage(ValidationError, 'тек өткізілген құжаттан'):
            post_document(self.sale_return(1, self.sale), self.seller)

    def test_return_with_wrong_base_type_is_rejected(self):
        receipt = Document.objects.get(doc_type=T.RECEIPT)

        with self.assertRaisesMessage(ValidationError, '«Сату» емес'):
            post_document(self.sale_return(1, receipt), self.seller)

    def test_return_to_other_warehouse_is_rejected(self):
        with self.assertRaisesMessage(ValidationError, 'қоймасынан жасалуы тиіс'):
            post_document(self.sale_return(1, self.sale, warehouse=self.other_warehouse), self.seller)

    def test_return_of_product_not_in_sale_is_rejected(self):
        with self.assertRaisesMessage(ValidationError, 'құжатында жоқ'):
            post_document(self.sale_return(1, self.sale, product=self.other_product), self.seller)

    def test_sale_with_posted_returns_cannot_be_canceled(self):
        post_document(self.sale_return(2, self.sale), self.seller)

        with self.assertRaisesMessage(ValidationError, 'өткізілген қайтарулары бар'):
            cancel_document(self.sale, self.manager, 'Қате енгізілген')

    def test_base_document_on_non_return_is_rejected(self):
        with self.assertRaisesMessage(ValidationError, 'тек қайтару құжаттарында'):
            post_document(self.make_doc(T.SALE, 1, base=self.sale), self.seller)


class SupplierReturnTests(ServiceTestCase):
    def setUp(self):
        self.receipt = self.receive(100)

    def supplier_return(self, qty, base=None, supplier=None, **kwargs):
        return self.make_doc(T.SUPPLIER_RETURN, qty, base=base, supplier=supplier or self.supplier, **kwargs)

    def test_return_within_received_quantity(self):
        post_document(self.supplier_return(5, self.receipt), self.warehouseman)

        self.assertEqual(self.stock(), 95)

    def test_return_requires_base_receipt(self):
        with self.assertRaisesMessage(ValidationError, 'негіз құжат'):
            post_document(self.supplier_return(1), self.warehouseman)

    def test_return_more_than_received_is_rejected(self):
        post_document(self.supplier_return(5, self.receipt), self.warehouseman)

        with self.assertRaisesMessage(ValidationError, 'қайтаруға болатыны 95.000'):
            post_document(self.supplier_return(96, self.receipt), self.warehouseman)

    def test_return_to_other_supplier_is_rejected(self):
        with self.assertRaisesMessage(ValidationError, 'жеткізушісіне («Серіктес ТОО»)'):
            post_document(self.supplier_return(1, self.receipt, supplier=self.other_supplier), self.warehouseman)

    def test_return_with_sale_as_base_is_rejected(self):
        sale = post_document(self.make_doc(T.SALE, 1), self.seller)

        with self.assertRaises(ValidationError) as ctx:
            post_document(self.supplier_return(1, sale), self.warehouseman)
        self.assertEqual(ctx.exception.messages, [f'№{sale.number} құжаты «Жеткізушіден қабылдау» емес.'])

    def test_receipt_with_posted_returns_cannot_be_canceled(self):
        post_document(self.supplier_return(5, self.receipt), self.warehouseman)

        with self.assertRaisesMessage(ValidationError, 'өткізілген қайтарулары бар'):
            cancel_document(self.receipt, self.manager, 'Қате енгізілген')

    def test_return_limited_by_stock_too(self):
        post_document(self.make_doc(T.SALE, 98), self.seller)

        with self.assertRaisesMessage(ValidationError, 'Қалдық жеткіліксіз'):
            post_document(self.supplier_return(5, self.receipt), self.warehouseman)


class DocumentAdminActionTests(ServiceTestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('admin')
        self.client.force_login(self.admin)
        self.url = reverse('admin:inventory_document_changelist')

    def run_action(self, action, *documents):
        return self.client.post(
            self.url, {'action': action, '_selected_action': [d.pk for d in documents]}, follow=True
        )

    def test_post_action_posts_valid_and_reports_invalid(self):
        receipt = self.make_doc(T.RECEIPT, 10, supplier=self.supplier)
        oversell = self.make_doc(T.SALE, 50)

        response = self.run_action('post_documents', receipt, oversell)

        receipt.refresh_from_db()
        oversell.refresh_from_db()
        self.assertEqual(receipt.status, S.POSTED)
        self.assertEqual(oversell.status, S.DRAFT)
        self.assertEqual(self.stock(), 10)
        messages = [str(m) for m in response.context['messages']]
        self.assertTrue(any('өткізілді' in m for m in messages))
        self.assertTrue(any('Қалдық жеткіліксіз' in m for m in messages))

    def test_cancel_action_asks_reason_then_reverses(self):
        receipt = self.receive(10)

        # 1-қадам: аралық бет — әлі ештеңе өзгермейді
        page = self.run_action('cancel_documents', receipt)
        self.assertTemplateUsed(page, 'admin/inventory/document/cancel_confirmation.html')
        self.assertContains(page, receipt.number)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, S.POSTED)

        # Себепсіз растау — форма қатесі, құжат өзгермейді
        empty = self.client.post(self.url, {
            'action': 'cancel_documents', '_selected_action': [receipt.pk], 'apply': '1', 'reason': '',
        })
        self.assertTrue(empty.context['form'].errors)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, S.POSTED)

        # 2-қадам: себеппен растау
        self.client.post(self.url, {
            'action': 'cancel_documents', '_selected_action': [receipt.pk], 'apply': '1', 'reason': 'Қате жеткізуші',
        }, follow=True)

        receipt.refresh_from_db()
        self.assertEqual(
            (receipt.status, receipt.canceled_by, receipt.cancel_reason), (S.CANCELED, self.admin, 'Қате жеткізуші')
        )
        self.assertEqual(self.stock(), 0)

    def test_cancel_action_reason_optional_for_drafts(self):
        draft = self.make_doc(T.RECEIPT, 10, supplier=self.supplier)

        self.client.post(self.url, {
            'action': 'cancel_documents', '_selected_action': [draft.pk], 'apply': '1', 'reason': '',
        }, follow=True)

        draft.refresh_from_db()
        self.assertEqual((draft.status, draft.canceled_by), (S.CANCELED, self.admin))


class AdminRoleTests(ServiceTestCase):
    """Админдегі рөлдік шектеулер (CLAUDE.md RBAC). Барлық қызметкерде is_staff бар."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for user in (cls.manager, cls.warehouseman, cls.seller):
            user.is_staff = True
            user.warehouse = cls.warehouse
            user.save()

    def login(self, user):
        self.client.force_login(user)

    def url(self, model, action='changelist', *args):
        return reverse(f'admin:inventory_{model}_{action}', args=args)

    def status(self, model, action='changelist', *args):
        return self.client.get(self.url(model, action, *args)).status_code

    def admin_receipt_data(self, doc_type=T.RECEIPT, warehouse=None, **extra):
        return {
            'number': 'ADM-1', 'doc_type': doc_type, 'warehouse': (warehouse or self.warehouse).pk,
            'supplier': self.supplier.pk, 'responsible': self.manager.pk,
            'items-TOTAL_FORMS': '1', 'items-INITIAL_FORMS': '0', 'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-product': self.product.pk, 'items-0-quantity': '10', 'items-0-price': '100',
            **extra,
        }

    def test_index_shows_only_allowed_models(self):
        expected = {
            self.manager: {'Тауарлар', 'Жеткізушілер', 'Қоймалар', 'Құжаттар', 'Қалдықтар', 'Қызметкерлер',
                           'Тауар қозғалыстары', 'Құжат жолдары'},
            self.warehouseman: {'Тауарлар', 'Жеткізушілер', 'Қоймалар', 'Құжаттар', 'Қалдықтар',
                                'Тауар қозғалыстары', 'Құжат жолдары'},
            self.seller: {'Тауарлар', 'Құжаттар', 'Қалдықтар'},
        }
        for user, models in expected.items():
            with self.subTest(role=user.role):
                self.login(user)
                app_list = self.client.get(reverse('admin:index')).context['app_list']
                self.assertEqual({m['name'] for app in app_list for m in app['models']}, models)

    def test_only_manager_edits_directories(self):
        for user in (self.warehouseman, self.seller):
            with self.subTest(role=user.role):
                self.login(user)
                self.assertEqual(self.status('product', 'add'), 403)
                response = self.client.post(
                    self.url('product', 'change', self.product.pk),
                    {'name': 'Hacked', 'sku': 'WATER-05', 'unit': 'PCS', 'sale_price': '1'},
                )
                self.assertEqual(response.status_code, 403)
        self.product.refresh_from_db()
        self.assertEqual(self.product.sale_price, Decimal('150'))

        self.login(self.manager)
        self.assertEqual(self.status('product', 'add'), 200)
        self.assertEqual(self.status('supplier', 'add'), 200)

    def test_only_superuser_edits_staff(self):
        self.login(self.manager)
        self.assertEqual(self.status('user'), 200)
        self.assertEqual(self.status('user', 'add'), 403)
        response = self.client.post(self.url('user', 'change', self.manager.pk), {'is_superuser': 'on'})
        self.assertEqual(response.status_code, 403)
        self.manager.refresh_from_db()
        self.assertFalse(self.manager.is_superuser)

        self.login(self.warehouseman)
        self.assertEqual(self.status('user'), 403)

    def test_warehouseman_creates_receipt_in_own_warehouse_as_responsible(self):
        self.login(self.warehouseman)

        response = self.client.post(self.url('document', 'add'), self.admin_receipt_data())

        self.assertEqual(response.status_code, 302)
        document = Document.objects.get(number='ADM-1')
        self.assertEqual((document.status, document.warehouse, document.responsible),
                         (S.DRAFT, self.warehouse, self.warehouseman))
        self.assertEqual(document.items.get().quantity, 10)

    def test_warehouseman_cannot_create_sale_or_use_other_warehouse(self):
        self.login(self.warehouseman)
        for data in (self.admin_receipt_data(doc_type=T.SALE),
                     self.admin_receipt_data(doc_type=T.INVENTORY),
                     self.admin_receipt_data(warehouse=self.other_warehouse)):
            with self.subTest(doc_type=data['doc_type'], warehouse=data['warehouse']):
                response = self.client.post(self.url('document', 'add'), data)
                self.assertEqual(response.status_code, 200)  # форма қатемен қайта көрсетіледі
                self.assertTrue(response.context['adminform'].form.errors)
        self.assertFalse(Document.objects.filter(number='ADM-1').exists())

    def test_warehouseman_sees_only_own_warehouse_documents(self):
        own = self.make_doc(T.RECEIPT, 1, supplier=self.supplier)
        foreign = self.make_doc(T.RECEIPT, 1, supplier=self.supplier, warehouse=self.other_warehouse)
        self.login(self.warehouseman)

        listed = list(self.client.get(self.url('document')).context['cl'].queryset)

        self.assertEqual(listed, [own])
        self.assertEqual(self.status('document', 'change', foreign.pk), 302)  # «табылмады» → индекске

    def test_posted_document_is_read_only_and_not_deletable(self):
        receipt = self.receive(10)
        self.login(self.manager)

        response = self.client.get(self.url('document', 'change', receipt.pk))

        self.assertFalse(response.context['has_change_permission'])
        self.assertEqual(self.status('document', 'delete', receipt.pk), 403)

    def test_warehouseman_cannot_edit_sale_draft_and_has_no_cancel_action(self):
        sale = self.make_doc(T.SALE, 1)
        self.login(self.warehouseman)

        response = self.client.get(self.url('document', 'change', sale.pk))
        actions = [name for name, _ in self.client.get(self.url('document')).context['action_form'].fields['action'].choices]

        self.assertFalse(response.context['has_change_permission'])
        self.assertIn('post_documents', actions)
        self.assertNotIn('cancel_documents', actions)

    def test_seller_only_views_own_sales(self):
        sale = self.make_doc(T.SALE, 1)
        self.make_doc(T.RECEIPT, 1, supplier=self.supplier)
        self.login(self.seller)

        listed = list(self.client.get(self.url('document')).context['cl'].queryset)

        self.assertEqual(listed, [sale])
        self.assertEqual(self.status('document', 'add'), 403)
        self.assertFalse(self.client.get(self.url('document', 'change', sale.pk)).context['has_change_permission'])
        self.assertEqual(self.client.get(self.url('document')).context['action_form'], None)

    def test_stock_and_movements_scoped_and_read_only(self):
        self.receive(10)
        self.receive(5, warehouse=self.other_warehouse)

        self.login(self.seller)
        self.assertEqual([s.quantity for s in self.client.get(self.url('stock')).context['cl'].queryset], [10])
        self.assertEqual(self.status('stockmovement'), 403)

        self.login(self.warehouseman)
        self.assertEqual(self.client.get(self.url('stockmovement')).context['cl'].result_count, 1)

        self.login(self.manager)
        self.assertEqual(self.client.get(self.url('stock')).context['cl'].result_count, 2)
        self.assertEqual(self.status('stock', 'add'), 403)


class ConcurrencyTestCase(ServiceMixin, TransactionTestCase):
    """Нақты параллель транзакциялар: әр ағынның өз DB қосылымы бар, барлығы Barrier арқылы бір мезетте басталады."""

    THREADS = 8

    def setUp(self):
        create_fixtures(self)

    def run_parallel(self, calls):
        barrier = threading.Barrier(len(calls))
        results = [None] * len(calls)

        def worker(index, call):
            try:
                barrier.wait()
                results[index] = ('ok', call())
            except Exception as e:
                results[index] = ('error', e)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(i, call)) for i, call in enumerate(calls)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        succeeded = [value for status, value in results if status == 'ok']
        errors = [value for status, value in results if status == 'error']
        # Deadlock немесе IntegrityError сияқты күтпеген қателер болмауы тиіс — тек бизнес-тексерулер
        unexpected = [e for e in errors if not isinstance(e, ValidationError)]
        self.assertEqual(unexpected, [])
        return succeeded, errors


class ConcurrencyTests(ConcurrencyTestCase):
    def test_parallel_sales_never_oversell(self):
        self.receive(10)
        sales = [self.make_doc(T.SALE, 3, user=self.seller) for _ in range(self.THREADS)]

        succeeded, errors = self.run_parallel([lambda d=d: post_document(d, self.seller) for d in sales])

        self.assertEqual(len(succeeded), 3)
        self.assertEqual(len(errors), self.THREADS - 3)
        self.assertEqual(self.stock(), 1)
        self.assertEqual(StockMovement.objects.filter(doc_type=T.SALE).count(), 3)
        self.assertEqual(Document.objects.filter(doc_type=T.SALE, status=S.POSTED).count(), 3)

    def test_same_document_posted_in_parallel_only_once(self):
        receipt = self.make_doc(T.RECEIPT, 10, supplier=self.supplier)

        succeeded, errors = self.run_parallel(
            [lambda: post_document(receipt, self.warehouseman)] * self.THREADS
        )

        self.assertEqual(len(succeeded), 1)
        self.assertEqual(len(errors), self.THREADS - 1)
        self.assertEqual(self.stock(), 10)
        self.assertEqual(StockMovement.objects.filter(document=receipt).count(), 1)

    def test_same_document_canceled_in_parallel_only_once(self):
        receipt = self.receive(10)

        succeeded, errors = self.run_parallel([lambda: cancel_document(receipt, self.manager, 'Қате енгізілген')] * self.THREADS)

        self.assertEqual(len(succeeded), 1)
        self.assertEqual(self.stock(), 0)
        self.assertEqual(StockMovement.objects.filter(document=receipt, is_reversal=True).count(), 1)

    def test_parallel_returns_never_exceed_sold_quantity(self):
        self.receive(100)
        sale = post_document(self.make_doc(T.SALE, 5), self.seller)
        returns = [self.make_doc(T.SALE_RETURN, 2, base=sale) for _ in range(self.THREADS)]

        succeeded, errors = self.run_parallel([lambda d=d: post_document(d, self.seller) for d in returns])

        self.assertEqual(len(succeeded), 2)
        self.assertEqual(self.stock(), 99)

    def test_parallel_first_receipts_create_single_stock_row(self):
        receipts = [self.make_doc(T.RECEIPT, 5, supplier=self.supplier) for _ in range(self.THREADS)]

        succeeded, errors = self.run_parallel([lambda d=d: post_document(d, self.warehouseman) for d in receipts])

        self.assertEqual(len(succeeded), self.THREADS)
        self.assertEqual(Stock.objects.filter(product=self.product, warehouse=self.warehouse).count(), 1)
        self.assertEqual(self.stock(), 5 * self.THREADS)

    def test_opposite_item_order_does_not_deadlock(self):
        self.receive(1000)
        self.receive(1000, product=self.other_product)
        sales = []
        for i in range(self.THREADS * 2):
            # Жартысында тауарлар A→B ретімен, жартысында B→A — бұғаттау реті сұрыпталмаса, deadlock болар еді
            first, second = (self.product, self.other_product) if i % 2 else (self.other_product, self.product)
            sale = self.make_doc(T.SALE, 1, product=first)
            DocumentItem.objects.create(document=sale, product=second, quantity=1, price=100)
            sales.append(sale)

        succeeded, errors = self.run_parallel([lambda d=d: post_document(d, self.seller) for d in sales])

        self.assertEqual(len(succeeded), len(sales))
        self.assertEqual(self.stock(), 1000 - len(sales))
        self.assertEqual(self.stock(self.other_product), 1000 - len(sales))


class PosApiTests(ServiceMixin, APITestCase):
    @classmethod
    def setUpTestData(cls):
        create_fixtures(cls)
        for user in (cls.seller, cls.manager, cls.warehouseman):
            user.warehouse = cls.warehouse
            user.save()

    def setUp(self):
        self.receive(100)
        self.client.force_authenticate(self.seller)

    def sell(self, *items, user=None):
        if user:
            self.client.force_authenticate(user)
        return self.client.post('/api/pos/sales/', {'items': list(items)}, format='json')

    def sell_water(self, qty, user=None):
        return self.sell({'barcode': '4870001112233', 'quantity': str(qty)}, user=user)

    def return_items(self, sale_id, *items):
        return self.client.post(f'/api/pos/sales/{sale_id}/returns/', {'items': list(items)}, format='json')

    def test_requires_authentication(self):
        self.client.force_authenticate(None)

        self.assertEqual(self.client.get('/api/pos/products/').status_code, 401)

    def test_token_auth(self):
        self.seller.set_password('secret-123')
        self.seller.save()
        self.client.force_authenticate(None)

        response = self.client.post('/api/auth/token/', {'username': 'seller', 'password': 'secret-123'})
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {response.data["token"]}')

        self.assertEqual(self.client.get('/api/pos/products/').status_code, 200)

    def test_products_show_available_stock_of_cashier_warehouse(self):
        self.receive(7, warehouse=self.other_warehouse)

        response = self.client.get('/api/pos/products/', {'search': 'Су'})

        self.assertEqual(response.status_code, 200)
        [product] = response.data['results']
        self.assertEqual(product['barcode'], '4870001112233')
        self.assertEqual(product['available'], '100.000')

    def test_product_without_stock_has_zero_available(self):
        response = self.client.get(f'/api/pos/products/{self.other_product.pk}/')

        self.assertEqual(response.data['available'], '0.000')

    def test_barcode_lookup(self):
        response = self.client.get('/api/pos/products/barcode/4870001112233/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['name'], 'Су 0.5л')
        self.assertEqual(self.client.get('/api/pos/products/barcode/000/').status_code, 404)

    def test_sale_is_created_and_posted_with_server_price(self):
        # Клиент жіберген баға еленбейді — баға тауардың сату бағасынан алынады
        response = self.sell({'barcode': '4870001112233', 'quantity': '3', 'price': '1'})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], S.POSTED)
        self.assertEqual(response.data['number'], f'SAL-{response.data["id"]:06d}')
        self.assertEqual(response.data['items'][0]['price'], '150.00')
        self.assertEqual(response.data['total'], '450.00')
        self.assertEqual(response.data['cashier'], 'seller')
        self.assertEqual(self.stock(), 97)

    def test_duplicate_lines_are_merged(self):
        response = self.sell(
            {'barcode': '4870001112233', 'quantity': '2'},
            {'product_id': self.product.pk, 'quantity': '3'},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data['items']), 1)
        self.assertEqual(response.data['items'][0]['quantity'], '5.000')
        self.assertEqual(self.stock(), 95)

    def test_oversell_returns_400_and_leaves_no_document(self):
        documents_before = Document.objects.count()

        response = self.sell_water(101)

        self.assertEqual(response.status_code, 400)
        self.assertIn('Қалдық жеткіліксіз:', response.data['detail'])
        self.assertEqual(Document.objects.count(), documents_before)
        self.assertEqual(self.stock(), 100)

    def test_invalid_lines_are_rejected(self):
        cases = [
            [],
            [{'quantity': '1'}],
            [{'barcode': '4870001112233', 'product_id': self.product.pk, 'quantity': '1'}],
            [{'barcode': 'unknown', 'quantity': '1'}],
            [{'barcode': '4870001112233', 'quantity': '0'}],
        ]
        for items in cases:
            with self.subTest(items=items):
                self.assertEqual(self.sell(*items).status_code, 400)
        self.assertEqual(self.stock(), 100)

    def test_warehouseman_cannot_sell(self):
        self.assertEqual(self.sell_water(1, user=self.warehouseman).status_code, 403)

    def test_cashier_without_warehouse_cannot_sell(self):
        self.seller.warehouse = None
        self.seller.save()

        response = self.sell_water(1)

        self.assertEqual(response.status_code, 400)
        self.assertIn('қойма бекітілмеген', response.data['detail'][0])

    def test_sale_during_inventory_is_rejected(self):
        self.make_doc(T.INVENTORY, 90)

        response = self.sell_water(1)

        self.assertEqual(response.status_code, 400)
        self.assertIn('түгендеуінде бұғатталған', response.data['detail'][0])

    def test_seller_sees_only_own_warehouse_sales(self):
        own = self.sell_water(1).data['id']
        other_seller = User.objects.create_user('other', role=User.Role.SELLER, warehouse=self.other_warehouse)
        self.receive(10, warehouse=self.other_warehouse)
        foreign = self.sell_water(1, user=other_seller).data['id']

        self.client.force_authenticate(self.seller)
        self.assertEqual([s['id'] for s in self.client.get('/api/pos/sales/').data['results']], [own])
        self.assertEqual(self.client.get(f'/api/pos/sales/{foreign}/').status_code, 404)

        self.client.force_authenticate(self.manager)
        self.assertEqual({s['id'] for s in self.client.get('/api/pos/sales/').data['results']}, {own, foreign})

    def test_search_sale_by_number(self):
        number = self.sell_water(1).data['number']
        self.sell_water(1)

        results = self.client.get('/api/pos/sales/', {'search': number}).data['results']

        self.assertEqual([sale['number'] for sale in results], [number])

    def test_return_within_sold_quantity(self):
        sale = self.sell_water(5).data

        response = self.return_items(sale['id'], {'barcode': '4870001112233', 'quantity': '2'})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['number'], f'SRT-{response.data["id"]:06d}')
        self.assertEqual(response.data['items'][0]['price'], '150.00')
        self.assertEqual(response.data['total'], '300.00')
        self.assertEqual(self.stock(), 97)

        detail = self.client.get(f'/api/pos/sales/{sale["id"]}/').data
        self.assertEqual(detail['returnable'], {str(self.product.pk): '3.000'})
        self.assertEqual([r['number'] for r in detail['returns']], [response.data['number']])

    def test_return_more_than_sold_is_rejected(self):
        sale = self.sell_water(5).data

        response = self.return_items(sale['id'], {'barcode': '4870001112233', 'quantity': '6'})

        self.assertEqual(response.status_code, 400)
        self.assertIn('қайтаруға болатыны 5.000', response.data['detail'][0])
        self.assertEqual(self.stock(), 95)

    def test_return_of_product_not_in_sale_is_rejected(self):
        sale = self.sell_water(5).data

        response = self.return_items(sale['id'], {'product_id': self.other_product.pk, 'quantity': '1'})

        self.assertEqual(response.status_code, 400)
        self.assertIn('құжатында жоқ', response.data['detail'][0])


class InventoryServiceTests(ServiceTestCase):
    def setUp(self):
        self.warehouseman.warehouse = self.warehouse
        self.warehouseman.save()

    def test_start_records_expected_quantity_and_requires_counts(self):
        self.receive(100)
        audit = start_inventory(self.warehouseman, [self.product])

        item = audit.items.get()
        self.assertEqual(audit.number, f'INV-{audit.pk:06d}')
        self.assertEqual(item.expected_quantity, 100)
        self.assertFalse(item.is_counted)
        with self.assertRaisesMessage(ValidationError, 'әлі саналмаған'):
            post_document(audit, self.manager)

    def test_cancel_of_posted_sale_is_blocked_during_inventory(self):
        self.receive(100)
        sale = post_document(self.make_doc(T.SALE, 10), self.seller)
        start_inventory(self.warehouseman, [self.product])

        with self.assertRaisesMessage(ValidationError, 'түгендеуінде бұғатталған'):
            cancel_document(sale, self.manager, 'Қате енгізілген')
        self.assertEqual(self.stock(), 90)

    def test_product_cannot_be_in_two_running_inventories(self):
        start_inventory(self.warehouseman, [self.product])

        with self.assertRaisesMessage(ValidationError, 'түгендеуінде тұр'):
            start_inventory(self.warehouseman, [self.product, self.other_product])

    def test_seller_cannot_start_inventory(self):
        self.seller.warehouse = self.warehouse
        self.seller.save()

        with self.assertRaises(PermissionDenied):
            start_inventory(self.seller, [self.product])


class InventoryApiTests(ServiceMixin, APITestCase):
    URL = '/api/inventory/audits/'

    @classmethod
    def setUpTestData(cls):
        create_fixtures(cls)
        for user in (cls.seller, cls.manager, cls.warehouseman):
            user.warehouse = cls.warehouse
            user.save()

    def setUp(self):
        self.receive(100)
        self.receive(20, product=self.other_product)
        self.client.force_authenticate(self.warehouseman)

    def start(self, payload=None):
        return self.client.post(self.URL, payload or {'product_ids': [self.product.pk]}, format='json')

    def count(self, audit_id, *items):
        return self.client.post(f'{self.URL}{audit_id}/counts/', {'items': list(items)}, format='json')

    def as_manager(self):
        self.client.force_authenticate(self.manager)

    def test_full_flow_start_count_approve(self):
        audit = self.start({'product_ids': [self.product.pk, self.other_product.pk]}).data
        self.assertEqual(audit['status'], S.DRAFT)
        self.assertEqual(audit['progress'], {'total': 2, 'counted': 0})

        response = self.count(
            audit['id'],
            {'barcode': '4870001112233', 'quantity': '97'},
            {'product_id': self.other_product.pk, 'quantity': '20'},
        )
        self.assertEqual(response.status_code, 200)
        items = {item['product_id']: item for item in response.data['items']}
        self.assertEqual(items[self.product.pk]['expected_quantity'], '100.000')
        self.assertEqual(items[self.product.pk]['counted_quantity'], '97.000')
        self.assertEqual(items[self.product.pk]['difference'], '-3.000')
        self.assertEqual(items[self.other_product.pk]['difference'], '0.000')

        self.as_manager()
        response = self.client.post(f'{self.URL}{audit["id"]}/approve/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], S.POSTED)
        self.assertEqual(self.stock(), 97)
        self.assertEqual(self.stock(self.other_product), 20)
        self.assertEqual(
            list(StockMovement.objects.filter(document_id=audit['id']).values_list('quantity', flat=True)),
            [Decimal(-3)],
        )

    def test_start_all_active_products(self):
        response = self.start({'all': True})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['progress']['total'], 2)

    def test_invalid_start_payloads(self):
        cases = [{}, {'product_ids': []}, {'product_ids': [999999]}, {'product_ids': [self.product.pk], 'all': True}]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post(self.URL, payload, format='json').status_code, 400)

    def test_running_inventory_blocks_pos_sale(self):
        self.start()
        self.client.force_authenticate(self.seller)

        response = self.client.post(
            '/api/pos/sales/', {'items': [{'barcode': '4870001112233', 'quantity': '1'}]}, format='json'
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('түгендеуінде бұғатталған', response.data['detail'][0])

    def test_recount_overwrites_previous_value(self):
        audit = self.start().data
        self.count(audit['id'], {'barcode': '4870001112233', 'quantity': '90'})

        response = self.count(audit['id'], {'barcode': '4870001112233', 'quantity': '0'})

        self.assertEqual(response.data['items'][0]['counted_quantity'], '0.000')

    def test_count_of_product_not_in_audit_is_rejected(self):
        audit = self.start().data

        response = self.count(audit['id'], {'product_id': self.other_product.pk, 'quantity': '1'})

        self.assertEqual(response.status_code, 400)
        self.assertIn('түгендеуінде жоқ', response.data['detail'][0])

    def test_approve_requires_all_counted(self):
        audit = self.start({'product_ids': [self.product.pk, self.other_product.pk]}).data
        self.count(audit['id'], {'barcode': '4870001112233', 'quantity': '97'})
        self.as_manager()

        response = self.client.post(f'{self.URL}{audit["id"]}/approve/')

        self.assertEqual(response.status_code, 400)
        self.assertIn('әлі саналмаған', response.data['detail'][0])
        self.assertEqual(self.stock(), 100)

    def test_warehouseman_cannot_approve(self):
        audit = self.start().data
        self.count(audit['id'], {'barcode': '4870001112233', 'quantity': '97'})

        response = self.client.post(f'{self.URL}{audit["id"]}/approve/')

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.stock(), 100)

    def test_counts_after_approve_are_rejected(self):
        audit = self.start().data
        self.count(audit['id'], {'barcode': '4870001112233', 'quantity': '97'})
        self.as_manager()
        self.client.post(f'{self.URL}{audit["id"]}/approve/')

        response = self.count(audit['id'], {'barcode': '4870001112233', 'quantity': '50'})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stock(), 97)

    def test_manager_cancels_running_audit_and_unblocks_sales(self):
        audit = self.start().data
        self.as_manager()

        response = self.client.post(f'{self.URL}{audit["id"]}/cancel/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], S.CANCELED)
        post_document(self.make_doc(T.SALE, 1), self.seller)
        self.assertEqual(self.stock(), 99)

    def test_seller_has_no_access(self):
        self.client.force_authenticate(self.seller)

        self.assertEqual(self.client.get(self.URL).status_code, 403)
        self.assertEqual(self.start().status_code, 403)

    def test_warehouseman_sees_only_own_warehouse(self):
        own = self.start().data['id']
        other = User.objects.create_user('wh2', role=User.Role.WAREHOUSEMAN, warehouse=self.other_warehouse)
        self.client.force_authenticate(other)
        foreign = self.start().data['id']

        self.assertEqual([a['id'] for a in self.client.get(self.URL).data['results']], [foreign])
        self.assertEqual(self.count(own, {'barcode': '4870001112233', 'quantity': '1'}).status_code, 404)

        self.as_manager()
        self.assertEqual({a['id'] for a in self.client.get(self.URL).data['results']}, {own, foreign})
        self.assertEqual(
            [a['id'] for a in self.client.get(self.URL, {'status': 'DRAFT'}).data['results'] if a['id'] == own], [own]
        )


class InventoryConcurrencyTests(ConcurrencyTestCase):
    """Түгендеу басталған сәтте қатар өтіп жатқан сатулар есептік қалдықты бұзбауы тиіс."""

    def test_sales_racing_with_inventory_start_never_change_snapshot(self):
        self.warehouseman.warehouse = self.warehouse
        self.warehouseman.save()
        self.receive(1000)
        sales = [self.make_doc(T.SALE, 1, user=self.seller) for _ in range(self.THREADS * 2)]
        calls = [lambda d=d: post_document(d, self.seller) for d in sales]
        calls.insert(len(calls) // 2, lambda: start_inventory(self.warehouseman, [self.product]))

        self.run_parallel(calls)

        # Түгендеу басталғаннан кейін өткен әр сату қалдықты есептік мәннен өзгертер еді
        audit = Document.objects.get(doc_type=T.INVENTORY)
        self.assertEqual(self.stock(), audit.items.get().expected_quantity)
        sold = Document.objects.filter(doc_type=T.SALE, status=S.POSTED).count()
        self.assertEqual(self.stock(), 1000 - sold)


class ReportApiTests(ServiceMixin, APITestCase):
    """Есептер: құжаттардың уақыты кезеңдерді тексеру үшін нақты күндерге жылжытылады."""

    @classmethod
    def setUpTestData(cls):
        create_fixtures(cls)
        for user in (cls.seller, cls.manager, cls.warehouseman):
            user.warehouse = cls.warehouse
            user.save()

    def setUp(self):
        self.client.force_authenticate(self.manager)

    def on(self, document, day):
        """Құжатты және оның қозғалыстарын 2026-09-<day> 12:00 уақытына жылжытады."""
        moment = datetime(2026, 9, day, 12, tzinfo=dt_timezone.utc)
        Document.objects.filter(pk=document.pk).update(posted_at=moment)
        StockMovement.objects.filter(document=document).update(date=moment)
        return document

    def get(self, name, **params):
        return self.client.get(f'/api/reports/{name}/', params)

    def build_history(self):
        """1-қыркүйек: +100 қабылдау; 10: сату 10 және 5; 11: 2 қайтару, 3 есептен шығару; 12: түгендеу 79 (−1)."""
        self.on(self.receive(100), 1)
        sale = self.on(post_document(self.make_doc(T.SALE, 10, price=150), self.seller), 10)
        self.on(post_document(self.make_doc(T.SALE, 5, price=150), self.seller), 10)
        canceled = post_document(self.make_doc(T.SALE, 1, price=150), self.seller)
        cancel_document(canceled, self.manager, 'Қате енгізілген')
        self.on(canceled, 10)
        self.on(post_document(self.make_doc(T.SALE_RETURN, 2, base=sale, price=150), self.seller), 11)
        self.on(post_document(self.make_doc(T.WRITE_OFF, 3, price=100), self.warehouseman), 11)
        self.on(post_document(self.make_doc(T.INVENTORY, 83, price=150), self.manager), 12)
        self.assertEqual(self.stock(), 83)

    def test_stock_report_totals_and_low_filter(self):
        self.receive(100)
        self.receive(5, product=self.other_product)

        report = self.get('stock').data

        self.assertEqual(report['totals'], {'positions': 2, 'value': '17500.00'})  # 100×150 + 5×500
        low = self.get('stock', low='10').data['rows']
        self.assertEqual([row['product_name'] for row in low], ['Шырын 1л'])

    def test_turnover_opening_incoming_outgoing_closing(self):
        self.build_history()

        report = self.get('turnover', date_from='2026-09-10', date_to='2026-09-12').data

        [row] = report['rows']
        # Бастапқы 100; кіріс: +1 кері (күші жойылған сату) +2 қайтару; шығыс: 10+5+1 сату, 3 шығару, 1 түгендеу
        self.assertEqual(
            (row['opening'], row['incoming'], row['outgoing'], row['closing']),
            ('100.000', '3.000', '20.000', '83.000'),
        )
        self.assertEqual(Decimal(row['closing']), self.stock())

    def test_turnover_before_any_movement_is_empty(self):
        self.build_history()

        self.assertEqual(self.get('turnover', date_from='2026-08-01', date_to='2026-08-31').data['rows'], [])

    def test_movements_journal_is_filtered_and_paginated(self):
        self.build_history()

        report = self.get('movements', date_from='2026-09-10', date_to='2026-09-10', doc_type='SALE').data

        self.assertEqual(report['count'], 4)  # 10, 5, 1 және оның кері жазбасы
        self.assertEqual(report['date_from'], date(2026, 9, 10))
        self.assertEqual({row['quantity'] for row in report['results']}, {'-10.000', '-5.000', '-1.000', '1.000'})

    def test_sales_report_by_day_excludes_canceled_and_subtracts_returns(self):
        self.build_history()

        report = self.get('sales', date_from='2026-09-01', date_to='2026-09-30').data

        self.assertEqual(
            [(row['day'], row['receipts'], row['sold_amount'], row['returned_amount']) for row in report['rows']],
            [(date(2026, 9, 10), 2, '2250.00', '0.00'), (date(2026, 9, 11), 0, '0.00', '300.00')],
        )
        self.assertEqual(report['totals']['net_amount'], '1950.00')

    def test_sales_report_by_product_and_cashier(self):
        self.build_history()
        period = {'date_from': '2026-09-01', 'date_to': '2026-09-30'}

        [by_product] = self.get('sales', group_by='product', **period).data['rows']
        [by_cashier] = self.get('sales', group_by='cashier', **period).data['rows']

        self.assertEqual((by_product['sold_qty'], by_product['returned_qty']), ('15.000', '2.000'))
        self.assertEqual((by_cashier['cashier'], by_cashier['net_amount']), ('manager', '1950.00'))

    def test_inventory_report_shortage_and_write_offs(self):
        self.build_history()

        report = self.get('inventory', date_from='2026-09-01', date_to='2026-09-30').data

        [audit] = report['audits']
        self.assertEqual(audit['lines'][0]['difference'], '-1.000')
        self.assertEqual(report['totals']['shortage_amount'], '150.00')
        self.assertEqual(report['totals']['surplus_qty'], '0.000')
        self.assertEqual(
            (report['write_offs']['total_quantity'], report['write_offs']['total_amount']), ('3.000', '300.00')
        )
        self.assertEqual({doc['reason'] for doc in report['write_offs']['documents']}, {'Бүлінген'})

    def test_warehouseman_sees_only_own_warehouse(self):
        self.receive(100)
        self.receive(7, warehouse=self.other_warehouse)
        self.client.force_authenticate(self.warehouseman)

        rows = self.get('stock').data['rows']

        self.assertEqual([row['warehouse'] for row in rows], ['Басты қойма'])
        self.assertEqual(self.get('stock', warehouse=self.other_warehouse.pk).status_code, 403)

    def test_manager_filters_by_warehouse(self):
        self.receive(100)
        self.receive(7, warehouse=self.other_warehouse)

        self.assertEqual(len(self.get('stock').data['rows']), 2)
        rows = self.get('stock', warehouse=self.other_warehouse.pk).data['rows']
        self.assertEqual([row['quantity'] for row in rows], ['7.000'])

    def test_access_by_role(self):
        self.client.force_authenticate(self.seller)
        for name in ('stock', 'movements', 'turnover', 'sales', 'inventory'):
            with self.subTest(report=name):
                self.assertEqual(self.get(name).status_code, 403)

        self.client.force_authenticate(self.warehouseman)
        self.assertEqual(self.get('sales').status_code, 403)
        self.assertEqual(self.get('turnover').status_code, 200)

    def test_invalid_period_is_rejected(self):
        self.assertEqual(self.get('turnover', date_from='2026-09-30', date_to='2026-09-01').status_code, 400)
        self.assertEqual(self.get('sales', group_by='week').status_code, 400)


class ApiSchemaTests(APITestCase):
    def test_schema_is_valid_and_has_no_warnings(self):
        # Жаңа endpoint құжаттамасыз (extend_schema-сыз) қосылса, осы тест құлайды
        call_command('spectacular', '--validate', '--fail-on-warn', stdout=io.StringIO(), stderr=io.StringIO())

    def test_schema_documents_all_endpoints(self):
        user = User.objects.create_user('docs')
        self.client.force_authenticate(user)

        schema = self.client.get('/api/schema/', HTTP_ACCEPT='application/vnd.oai.openapi+json').json()

        self.assertTrue({
            '/api/auth/token/', '/api/pos/sales/', '/api/pos/sales/{id}/returns/',
            '/api/inventory/audits/{id}/approve/', '/api/reports/sales/', '/api/reports/turnover/',
        } <= set(schema['paths']))
        sale_request = schema['paths']['/api/pos/sales/']['post']['requestBody']['content']['application/json']
        self.assertEqual(sale_request['schema']['$ref'], '#/components/schemas/LinesRequest')
        self.assertIn('tokenAuth', schema['components']['securitySchemes'])

    def test_health_is_public(self):
        response = self.client.get('/api/health/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'status': 'ok', 'database': 'ok'})

    def test_docs_pages_require_login(self):
        for url in ('/api/docs/', '/api/redoc/', '/api/schema/'):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 401)
        self.client.force_authenticate(User.objects.create_user('docs'))
        self.assertEqual(self.client.get('/api/docs/').status_code, 200)
        self.assertEqual(self.client.get('/api/redoc/').status_code, 200)


class DocumentCommentTests(ServiceMixin, APITestCase):
    @classmethod
    def setUpTestData(cls):
        create_fixtures(cls)
        for user in (cls.seller, cls.manager, cls.warehouseman):
            user.warehouse = cls.warehouse
            user.save()

    def setUp(self):
        self.receive(100)

    def test_write_off_without_reason_is_rejected(self):
        for blank in ('', '   '):
            with self.subTest(comment=blank):
                document = self.make_doc(T.WRITE_OFF, 2, comment=blank)

                with self.assertRaisesMessage(ValidationError, 'себебі көрсетілуі тиіс'):
                    post_document(document, self.warehouseman)
                self.assertEqual(self.stock(), 100)

    def test_write_off_with_reason_is_posted(self):
        document = post_document(self.make_doc(T.WRITE_OFF, 2, comment='Мерзімі өткен'), self.warehouseman)

        self.assertEqual(document.status, S.POSTED)
        self.assertEqual(Document.objects.get(pk=document.pk).comment, 'Мерзімі өткен')
        self.assertEqual(self.stock(), 98)

    def test_other_documents_do_not_require_comment(self):
        post_document(self.make_doc(T.RECEIPT, 1, supplier=self.supplier, comment=''), self.warehouseman)

        self.assertEqual(self.stock(), 101)

    def test_model_clean_requires_write_off_reason(self):
        # Админ формасы model.full_clean() шақырады — қате comment өрісінің жанында шығады
        document = Document(number='X-1', doc_type=T.WRITE_OFF, warehouse=self.warehouse, responsible=self.manager)

        with self.assertRaises(ValidationError) as ctx:
            document.full_clean()
        self.assertIn('comment', ctx.exception.message_dict)

    def test_sale_return_reason_via_api(self):
        self.client.force_authenticate(self.seller)
        sale = self.client.post('/api/pos/sales/', {'items': [{'product_id': self.product.pk, 'quantity': '2'}]},
                                format='json').data

        response = self.client.post(f'/api/pos/sales/{sale["id"]}/returns/', {
            'items': [{'product_id': self.product.pk, 'quantity': '1'}], 'comment': '  Ақаулы қақпақ  ',
        }, format='json')

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['comment'], 'Ақаулы қақпақ')
        sale = self.client.get(f'/api/pos/sales/{sale["id"]}/').data
        self.assertEqual(sale['returns'][0]['comment'], 'Ақаулы қақпақ')

    def test_inventory_comment_on_start_and_approve(self):
        self.client.force_authenticate(self.warehouseman)
        audit = self.client.post('/api/inventory/audits/', {
            'product_ids': [self.product.pk], 'comment': 'Жоспарлы',
        }, format='json').data
        self.assertEqual(audit['comment'], 'Жоспарлы')
        self.client.post(f'/api/inventory/audits/{audit["id"]}/counts/', {
            'items': [{'product_id': self.product.pk, 'quantity': '99'}],
        }, format='json')

        self.client.force_authenticate(self.manager)
        response = self.client.post(f'/api/inventory/audits/{audit["id"]}/approve/',
                                    {'comment': 'Бір дана ұрланған'}, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['comment'], 'Бір дана ұрланған')
        report = self.client.get('/api/reports/inventory/').data
        self.assertEqual(report['audits'][0]['comment'], 'Бір дана ұрланған')
        movements = self.client.get('/api/reports/movements/', {'doc_type': T.INVENTORY}).data['results']
        self.assertEqual(movements[0]['comment'], 'Бір дана ұрланған')

    def test_approve_without_comment_keeps_start_comment(self):
        audit = start_inventory(self.warehouseman, [self.product], 'Жоспарлы')
        DocumentItem.objects.filter(document=audit).update(quantity=100, is_counted=True)
        self.client.force_authenticate(self.manager)

        response = self.client.post(f'/api/inventory/audits/{audit.pk}/approve/', format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['comment'], 'Жоспарлы')

    def test_comment_too_long_is_rejected(self):
        self.client.force_authenticate(self.warehouseman)

        response = self.client.post('/api/inventory/audits/', {
            'product_ids': [self.product.pk], 'comment': 'x' * 501,
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Document.objects.filter(doc_type=T.INVENTORY).exists())


class WriteOffApiTests(ServiceMixin, APITestCase):
    URL = '/api/warehouse/write-offs/'

    @classmethod
    def setUpTestData(cls):
        create_fixtures(cls)
        for user in (cls.seller, cls.manager, cls.warehouseman):
            user.warehouse = cls.warehouse
            user.save()

    def setUp(self):
        self.receive(10)
        self.client.force_authenticate(self.warehouseman)

    def write_off(self, quantity='2', comment='Бүлінген', **line):
        payload = {'items': [line or {'barcode': '4870001112233', 'quantity': quantity}]}
        if comment is not None:
            payload['comment'] = comment
        return self.client.post(self.URL, payload, format='json')

    def test_write_off_decreases_stock_and_logs_movement(self):
        response = self.write_off('2', '  Бөтелке жарылған  ')

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], S.POSTED)
        self.assertTrue(response.data['number'].startswith('WOF-'))
        self.assertEqual(response.data['comment'], 'Бөтелке жарылған')
        self.assertEqual(response.data['total'], '300.00')  # 2 × сату бағасы 150
        self.assertEqual(response.data['responsible'], 'warehouseman')
        self.assertEqual(self.stock(), 8)
        movement = StockMovement.objects.get(document_id=response.data['id'])
        self.assertEqual((movement.quantity, movement.user), (Decimal('-2'), self.warehouseman))

    def test_reason_is_required(self):
        for comment in (None, '', '   '):
            with self.subTest(comment=comment):
                response = self.write_off(comment=comment)

                self.assertEqual(response.status_code, 400)
                self.assertIn('comment', response.data)
        self.assertEqual(self.stock(), 10)
        self.assertFalse(Document.objects.filter(doc_type=T.WRITE_OFF).exists())

    def test_insufficient_stock_saves_nothing(self):
        response = self.write_off('11')

        self.assertEqual(response.status_code, 400)
        self.assertIn('Қалдық жеткіліксіз', ' '.join(response.data['detail']))
        self.assertEqual(self.stock(), 10)
        self.assertFalse(Document.objects.filter(doc_type=T.WRITE_OFF).exists())

    def test_blocked_by_inventory(self):
        start_inventory(self.warehouseman, [self.product])

        response = self.write_off('1')

        self.assertEqual(response.status_code, 400)
        self.assertIn('бұғатталған', ' '.join(response.data['detail']))
        self.assertEqual(self.stock(), 10)

    def test_seller_is_forbidden(self):
        self.client.force_authenticate(self.seller)

        self.assertEqual(self.write_off().status_code, 403)
        self.assertEqual(self.client.get(self.URL).status_code, 403)
        self.assertEqual(self.stock(), 10)

    def test_warehouseman_sees_only_own_warehouse(self):
        self.write_off('1')
        other = User.objects.create_user('other', role=User.Role.WAREHOUSEMAN, warehouse=self.other_warehouse)
        self.receive(5, warehouse=self.other_warehouse)
        self.client.force_authenticate(other)
        self.write_off('1')

        self.assertEqual(self.client.get(self.URL).data['count'], 1)
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.client.get(self.URL).data['count'], 2)

    def test_shows_in_inventory_report(self):
        self.write_off('3', 'Мерзімі өткен')
        self.client.force_authenticate(self.manager)

        report = self.client.get('/api/reports/inventory/').data['write_offs']

        self.assertEqual(report['total_quantity'], '3.000')
        self.assertEqual([d['reason'] for d in report['documents']], ['Мерзімі өткен'])


class ReceiptApiTests(ServiceMixin, APITestCase):
    URL = '/api/warehouse/receipts/'

    @classmethod
    def setUpTestData(cls):
        create_fixtures(cls)
        for user in (cls.seller, cls.manager, cls.warehouseman):
            user.warehouse = cls.warehouse
            user.save()

    def setUp(self):
        self.client.force_authenticate(self.warehouseman)

    def receipt(self, *items, supplier=None, comment=''):
        items = items or ({'barcode': '4870001112233', 'quantity': '10', 'price': '90.50'},)
        return self.client.post(self.URL, {
            'supplier_id': (supplier or self.supplier).pk, 'items': list(items), 'comment': comment,
        }, format='json')

    def give_back(self, receipt_id, quantity, comment=''):
        return self.client.post(f'{self.URL}{receipt_id}/returns/', {
            'items': [{'product_id': self.product.pk, 'quantity': quantity}], 'comment': comment,
        }, format='json')

    def test_receipt_increases_stock_with_purchase_price(self):
        response = self.receipt(comment='Накладной №17')

        self.assertEqual(response.status_code, 201)
        data = response.data
        self.assertTrue(data['number'].startswith('RCV-'))
        self.assertEqual((data['status'], data['supplier'], data['comment']), (S.POSTED, 'Серіктес ТОО', 'Накладной №17'))
        self.assertEqual(data['items'][0]['price'], '90.50')
        self.assertEqual(data['total'], '905.00')
        self.assertEqual(data['returnable'], {str(self.product.pk): '10.000'})
        self.assertEqual(self.stock(), 10)

    def test_duplicate_lines_are_merged_but_price_must_match(self):
        same = {'product_id': self.product.pk, 'quantity': '2', 'price': '90'}

        self.assertEqual(self.receipt(same, same).data['items'][0]['quantity'], '4.000')
        response = self.receipt(same, {**same, 'price': '95'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('әртүрлі бағамен', ' '.join(response.data['detail']))
        self.assertEqual(self.stock(), 4)

    def test_invalid_input_is_rejected(self):
        line = {'product_id': self.product.pk, 'quantity': '1', 'price': '10'}
        cases = {
            'no price': {'supplier_id': self.supplier.pk, 'items': [{'product_id': self.product.pk, 'quantity': '1'}]},
            'negative price': {'supplier_id': self.supplier.pk, 'items': [{**line, 'price': '-1'}]},
            'unknown supplier': {'supplier_id': 999999, 'items': [line]},
            'no supplier': {'items': [line]},
            'no items': {'supplier_id': self.supplier.pk, 'items': []},
        }
        for name, payload in cases.items():
            with self.subTest(name):
                self.assertEqual(self.client.post(self.URL, payload, format='json').status_code, 400)
        self.assertFalse(Document.objects.exists())

    def test_supplier_return_uses_receipt_supplier_and_price(self):
        receipt = self.receipt().data

        response = self.give_back(receipt['id'], '3', 'Ақаулы партия')

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data['number'].startswith('RTS-'))
        self.assertEqual(response.data['items'][0]['price'], '90.50')
        self.assertEqual(response.data['comment'], 'Ақаулы партия')
        document = Document.objects.get(pk=response.data['id'])
        self.assertEqual((document.supplier, document.base_document_id), (self.supplier, receipt['id']))
        self.assertEqual(self.stock(), 7)
        self.assertEqual(self.client.get(f'{self.URL}{receipt["id"]}/').data['returnable'],
                         {str(self.product.pk): '7.000'})

    def test_supplier_return_cannot_exceed_received(self):
        receipt = self.receipt().data
        self.give_back(receipt['id'], '8')

        response = self.give_back(receipt['id'], '3')

        self.assertEqual(response.status_code, 400)
        self.assertIn('қайтаруға болатыны 2', ' '.join(response.data['detail']))
        self.assertEqual(self.stock(), 2)

    def test_supplier_return_cannot_exceed_stock(self):
        receipt = self.receipt().data
        self.client.force_authenticate(self.manager)
        self.client.post('/api/pos/sales/', {'items': [{'product_id': self.product.pk, 'quantity': '9'}]},
                         format='json')
        self.client.force_authenticate(self.warehouseman)

        response = self.give_back(receipt['id'], '5')

        self.assertEqual(response.status_code, 400)
        self.assertIn('Қалдық жеткіліксіз', ' '.join(response.data['detail']))
        self.assertEqual(self.stock(), 1)

    def test_receipt_blocked_by_inventory(self):
        self.receipt()
        start_inventory(self.warehouseman, [self.product])

        response = self.receipt()

        self.assertEqual(response.status_code, 400)
        self.assertIn('бұғатталған', ' '.join(response.data['detail']))
        self.assertEqual(self.stock(), 10)

    def test_access(self):
        self.client.force_authenticate(self.seller)
        self.assertEqual(self.receipt().status_code, 403)
        self.assertEqual(self.client.get(self.URL).status_code, 403)
        self.assertEqual(self.client.get('/api/warehouse/suppliers/').status_code, 403)

        other = User.objects.create_user('other', role=User.Role.WAREHOUSEMAN, warehouse=self.other_warehouse)
        self.client.force_authenticate(self.warehouseman)
        receipt = self.receipt().data
        self.client.force_authenticate(other)
        # Басқа қойманың қабылдауы көрінбейді және одан қайтаруға болмайды
        self.assertEqual(self.client.get(self.URL).data['count'], 0)
        self.assertEqual(self.give_back(receipt['id'], '1').status_code, 404)
        self.assertEqual(self.stock(), 10)

    def test_supplier_search(self):
        response = self.client.get('/api/warehouse/suppliers/', {'search': 'Серік'})

        self.assertEqual([s['name'] for s in response.data['results']], ['Серіктес ТОО'])


class CancelReasonTests(ServiceMixin, APITestCase):
    @classmethod
    def setUpTestData(cls):
        create_fixtures(cls)
        for user in (cls.seller, cls.manager, cls.warehouseman):
            user.warehouse = cls.warehouse
            user.save()

    def test_posted_requires_reason(self):
        receipt = self.receive(10)

        for blank in ('', '   '):
            with self.subTest(reason=blank):
                with self.assertRaisesMessage(ValidationError, 'себебі көрсетілуі тиіс'):
                    cancel_document(receipt, self.manager, blank)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, S.POSTED)
        self.assertEqual(self.stock(), 10)
        self.assertFalse(StockMovement.objects.filter(is_reversal=True).exists())

    def test_who_and_why_are_stored(self):
        receipt = self.receive(10)

        cancel_document(receipt, self.manager, '  Қате жеткізуші  ')

        receipt.refresh_from_db()
        self.assertEqual((receipt.canceled_by, receipt.cancel_reason), (self.manager, 'Қате жеткізуші'))
        self.assertIsNotNone(receipt.canceled_at)

    def test_permission_checked_before_reason(self):
        # Басқарушы емес қызметкерге «себеп жоқ» емес, «рұқсат жоқ» деп жауап беру керек
        receipt = self.receive(1)

        with self.assertRaises(PermissionDenied):
            cancel_document(receipt, self.warehouseman)

    def test_draft_reason_is_optional(self):
        draft = self.make_doc(T.WRITE_OFF, 1)

        cancel_document(draft, self.warehouseman)

        draft.refresh_from_db()
        self.assertEqual((draft.status, draft.canceled_by, draft.cancel_reason), (S.CANCELED, self.warehouseman, ''))

    def test_inventory_cancel_api(self):
        self.receive(10)
        self.client.force_authenticate(self.manager)
        audit = start_inventory(self.manager, [self.product])
        DocumentItem.objects.filter(document=audit).update(quantity=8, is_counted=True)
        post_document(audit, self.manager)
        url = f'/api/inventory/audits/{audit.pk}/cancel/'

        self.assertEqual(self.client.post(url, format='json').status_code, 400)
        response = self.client.post(url, {'reason': 'Қайта санау керек'}, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            (response.data['status'], response.data['canceled_by'], response.data['cancel_reason']),
            (S.CANCELED, 'manager', 'Қайта санау керек'),
        )
        self.assertEqual(self.stock(), 10)
        movements = self.client.get('/api/reports/movements/', {'doc_type': T.INVENTORY}).data['results']
        reversal = next(m for m in movements if m['is_reversal'])
        self.assertEqual((reversal['cancel_reason'], reversal['user']), ('Қайта санау керек', 'manager'))
        self.assertEqual(next(m for m in movements if not m['is_reversal'])['cancel_reason'], '')

    def test_canceled_return_shows_in_sale(self):
        self.receive(10)
        self.client.force_authenticate(self.seller)
        sale = self.client.post('/api/pos/sales/', {'items': [{'product_id': self.product.pk, 'quantity': '2'}]},
                                format='json').data
        sale_return = self.client.post(f'/api/pos/sales/{sale["id"]}/returns/', {
            'items': [{'product_id': self.product.pk, 'quantity': '1'}],
        }, format='json').data
        cancel_document(Document.objects.get(pk=sale_return['id']), self.manager, 'Клиент тауарды қайтып алды')

        data = self.client.get(f'/api/pos/sales/{sale["id"]}/').data

        self.assertEqual(data['returns'][0]['canceled_by'], 'manager')
        self.assertEqual(data['returns'][0]['cancel_reason'], 'Клиент тауарды қайтып алды')
        self.assertIsNone(data['canceled_by'])
        self.assertEqual(data['returnable'], {str(self.product.pk): '2.000'})


class DocumentApiTests(ServiceMixin, APITestCase):
    URL = '/api/documents/'

    @classmethod
    def setUpTestData(cls):
        create_fixtures(cls)
        for user in (cls.seller, cls.manager, cls.warehouseman):
            user.warehouse = cls.warehouse
            user.save()

    def setUp(self):
        self.client.force_authenticate(self.manager)

    def cancel(self, document, reason=None):
        body = {} if reason is None else {'reason': reason}
        return self.client.post(f'{self.URL}{document.pk}/cancel/', body, format='json')

    def test_only_manager(self):
        receipt = self.receive(10)
        for user in (self.seller, self.warehouseman):
            with self.subTest(role=user.role):
                self.client.force_authenticate(user)
                self.assertEqual(self.client.get(self.URL).status_code, 403)
                self.assertEqual(self.cancel(receipt, 'x').status_code, 403)
        self.assertEqual(self.stock(), 10)

    def test_journal_lists_all_types_with_filters(self):
        receipt = self.receive(10)
        sale = post_document(self.make_doc(T.SALE, 2), self.seller)
        post_document(self.make_doc(T.WRITE_OFF, 1), self.warehouseman)

        data = self.client.get(self.URL).data
        self.assertEqual(data['count'], 3)
        self.assertEqual(
            {d['doc_type'] for d in data['results']}, {T.RECEIPT, T.SALE, T.WRITE_OFF}
        )
        sales = self.client.get(self.URL, {'doc_type': T.SALE}).data['results']
        self.assertEqual([d['number'] for d in sales], [sale.number])
        self.assertEqual(self.client.get(self.URL, {'search': receipt.number}).data['count'], 1)
        self.assertEqual(self.client.get(self.URL, {'status': S.CANCELED}).data['count'], 0)
        self.assertEqual(self.client.get(self.URL, {'date_from': '2000-01-01', 'date_to': '2000-01-31'}).data['count'], 0)
        self.assertEqual(self.client.get(self.URL, {'doc_type': 'BAD'}).status_code, 400)
        self.assertEqual(self.client.get(self.URL, {'date_from': '2026-02-01', 'date_to': '2026-01-01'}).status_code, 400)

    def test_cancel_sale_restores_stock(self):
        self.receive(10)
        sale = post_document(self.make_doc(T.SALE, 3), self.seller)

        self.assertEqual(self.cancel(sale).status_code, 400)
        self.assertEqual(self.stock(), 7)
        response = self.cancel(sale, 'Кассир қате тауар соқты')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            (response.data['status'], response.data['canceled_by'], response.data['cancel_reason']),
            (S.CANCELED, 'manager', 'Кассир қате тауар соқты'),
        )
        self.assertEqual(self.stock(), 10)

    def test_cancel_draft_without_reason(self):
        draft = self.make_doc(T.RECEIPT, 5, supplier=self.supplier)

        response = self.cancel(draft)

        self.assertEqual((response.status_code, response.data['status']), (200, S.CANCELED))

    def test_cannot_cancel_base_with_posted_returns(self):
        receipt = self.receive(10)
        supplier_return = post_document(
            self.make_doc(T.SUPPLIER_RETURN, 2, supplier=self.supplier, base=receipt), self.warehouseman
        )

        response = self.cancel(receipt, 'Қате')

        self.assertEqual(response.status_code, 400)
        self.assertIn(supplier_return.number, ' '.join(response.data['detail']))
        detail = self.client.get(f'{self.URL}{receipt.pk}/').data
        self.assertEqual([r['number'] for r in detail['returns']], [supplier_return.number])
        # Алдымен қайтарудың, содан кейін қабылдаудың күшін жоюға болады
        self.assertEqual(self.cancel(supplier_return, 'Қате').status_code, 200)
        self.assertEqual(self.cancel(receipt, 'Қате').status_code, 200)
        self.assertEqual(self.stock(), 0)

    def test_cannot_cancel_receipt_if_goods_already_sold(self):
        receipt = self.receive(10)
        post_document(self.make_doc(T.SALE, 8), self.seller)

        response = self.cancel(receipt, 'Қате')

        self.assertEqual(response.status_code, 400)
        self.assertIn('Қалдық жеткіліксіз', ' '.join(response.data['detail']))
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, S.POSTED)
        self.assertEqual(self.stock(), 2)

    def test_detail_shows_base_document(self):
        receipt = self.receive(10)
        supplier_return = post_document(
            self.make_doc(T.SUPPLIER_RETURN, 2, supplier=self.supplier, base=receipt), self.warehouseman
        )

        data = self.client.get(f'{self.URL}{supplier_return.pk}/').data

        self.assertEqual(data['base_document']['number'], receipt.number)
        self.assertEqual((data['supplier'], data['doc_type_display']), ('Серіктес ТОО', 'Жеткізушіге қайтару'))


class LocalizationTests(ServiceMixin, APITestCase):
    """Django/DRF стандарт хабарлары қазақша (LANGUAGE_CODE='kk')."""

    @classmethod
    def setUpTestData(cls):
        create_fixtures(cls)
        cls.warehouseman.warehouse = cls.warehouse
        cls.warehouseman.save()

    def test_field_errors_are_in_kazakh(self):
        self.client.force_authenticate(self.warehouseman)

        response = self.client.post('/api/warehouse/write-offs/', {
            'items': [{'product_id': self.product.pk, 'quantity': '-1'}],
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['comment'], ['Бұл мән міндетті.'])
        self.assertIn('кемінде', response.data['items'][0]['quantity'][0])

    def test_auth_errors_are_in_kazakh(self):
        response = self.client.get('/api/warehouse/write-offs/')

        self.assertEqual(response.status_code, 401)
        self.assertNotIn('credentials', str(response.data['detail']))

    def test_broken_json_has_readable_message(self):
        self.client.force_authenticate(self.warehouseman)

        response = self.client.post('/api/warehouse/write-offs/', b'{"items": [', content_type='application/json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('JSON', response.data['detail'])
        self.assertIn('UTF-8', response.data['detail'])

    def test_admin_login_page_is_in_kazakh(self):
        # Бұрын кірмеген пайдаланушыда кіру беті 500 беретін (AnonymousUser-де role жоқ)
        response = self.client.get(reverse('admin:login'))

        self.assertContains(response, 'lang="kk"')
        self.assertRedirects(self.client.get(reverse('admin:index')), f'{reverse("admin:login")}?next=/admin/')


class BackupTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def touch(self, name):
        path = self.dir / name
        path.write_bytes(b'x')
        return path

    def test_prune_removes_only_old_backups(self):
        now = datetime(2026, 10, 28, 12, 0)
        old = self.touch('store_20261001_020000.dump')
        recent = self.touch('store_20261027_020000.dump')
        foreign = self.touch('store_manual.dump')

        removed = backup.prune_backups(14, self.dir, now=now)

        self.assertEqual(removed, [old])
        self.assertTrue(recent.exists())
        self.assertTrue(foreign.exists())

    def test_prune_keeps_newest_even_if_old(self):
        # Көшірме ұзақ жасалмай қалса да, соңғысы өшірілмеуі тиіс
        newest = self.touch('store_20260101_020000.dump')
        older = self.touch('store_20251201_020000.dump')

        removed = backup.prune_backups(14, self.dir, now=datetime(2026, 10, 28))

        self.assertEqual(removed, [older])
        self.assertTrue(newest.exists())

    def test_prune_disabled_with_zero(self):
        self.touch('store_20200101_020000.dump')
        self.touch('store_20200102_020000.dump')

        self.assertEqual(backup.prune_backups(0, self.dir), [])
        self.assertEqual(len(backup.list_backups(self.dir)), 2)

    def test_failed_dump_leaves_no_file(self):
        def fail(args):
            Path(args[args.index('--file') + 1]).write_bytes(b'half')  # pg_dump жартылай жазып үлгерді
            raise backup.BackupError('pg_dump қатемен аяқталды')

        with mock.patch.object(backup, '_run', side_effect=fail), mock.patch.object(backup, '_tool', return_value='pg'):
            with self.assertRaises(backup.BackupError):
                backup.create_backup(self.dir)

        self.assertEqual(list(self.dir.iterdir()), [])

    def test_password_not_in_command_line(self):
        with mock.patch.object(backup.subprocess, 'run') as run, mock.patch.object(backup, '_tool', return_value='pg'):
            run.return_value.returncode = 0
            backup.restore_backup(self.touch('store_20261028_020000.dump'))

        args, kwargs = run.call_args
        password = connection.settings_dict['PASSWORD']
        self.assertNotIn(password, ' '.join(args[0]))
        self.assertEqual(kwargs['env']['PGPASSWORD'], password)
        self.assertIn('--single-transaction', args[0])

    def test_restore_requires_confirmation(self):
        path = self.touch('store_20261028_020000.dump')
        with mock.patch('inventory.management.commands.restore_db.restore_backup') as restore, \
                mock.patch('inventory.management.commands.restore_db.create_backup') as safety:
            with self.assertRaisesMessage(CommandError, '--yes'):
                call_command('restore_db', str(path), stdout=io.StringIO())
        restore.assert_not_called()
        safety.assert_not_called()

    def test_restore_makes_safety_backup_first(self):
        path = self.touch('store_20261028_020000.dump')
        calls = mock.Mock()
        with mock.patch('inventory.management.commands.restore_db.create_backup', calls.backup), \
                mock.patch('inventory.management.commands.restore_db.restore_backup', calls.restore):
            call_command('restore_db', str(path), '--yes', stdout=io.StringIO())

        self.assertEqual([c[0] for c in calls.mock_calls], ['backup', 'restore'])

    @skipUnless(shutil.which('pg_dump', path=settings.BACKUP_PG_BIN or None), 'pg_dump табылмады')
    def test_real_backup_is_valid_archive(self):
        out = io.StringIO()

        call_command('backup_db', '--dir', str(self.dir), '--keep-days', '0', stdout=out)

        [dump] = backup.list_backups(self.dir)
        self.assertGreater(dump.stat().st_size, 0)
        self.assertIn('Көшірме жасалды', out.getvalue())
        # Архив ішінде қоймалық кестелер бар
        listing = backup._run([backup._tool('pg_restore'), '--list', str(dump)]).stdout
        self.assertIn('inventory_stockmovement', listing)
