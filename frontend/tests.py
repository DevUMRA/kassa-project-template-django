from django.test import TestCase
from rest_framework.test import APIClient

from inventory.models import User, Warehouse
from inventory.tests import ServiceMixin, create_fixtures


class FrontendPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.warehouse = Warehouse.objects.create(name='Басты қойма')
        cls.seller = User.objects.create_user('seller', password='secret-123', role=User.Role.SELLER, warehouse=cls.warehouse)

    def test_app_requires_login(self):
        response = self.client.get('/')

        self.assertRedirects(response, '/login/?next=/')

    def test_login_and_logout(self):
        response = self.client.post('/login/', {'username': 'seller', 'password': 'secret-123'})
        self.assertRedirects(response, '/')

        page = self.client.get('/')
        self.assertContains(page, 'frontend/app.js')
        self.assertContains(page, 'window.CSRF_TOKEN')
        self.assertIn('csrftoken', page.cookies)

        self.assertRedirects(self.client.post('/logout/'), '/login/')
        self.assertRedirects(self.client.get('/'), '/login/?next=/')

    def test_wrong_password_shows_error(self):
        response = self.client.post('/login/', {'username': 'seller', 'password': 'wrong'})

        self.assertContains(response, 'Логин немесе құпия сөз қате.')


class MeEndpointTests(ServiceMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        create_fixtures(cls)
        cls.seller.warehouse = cls.warehouse
        cls.seller.save()

    def test_sections_by_role(self):
        expected = {
            self.seller: ['pos'],
            self.warehouseman: ['warehouse', 'inventory', 'reports'],
            self.manager: ['pos', 'warehouse', 'inventory', 'documents', 'reports'],
        }
        for user, sections in expected.items():
            with self.subTest(role=user.role):
                self.client.force_login(user)
                self.assertEqual(self.client.get('/api/me/').json()['sections'], sections)

    def test_me_shows_warehouse(self):
        self.client.force_login(self.seller)

        data = self.client.get('/api/me/').json()

        self.assertEqual(data['warehouse'], {'id': self.warehouse.pk, 'name': 'Басты қойма'})
        self.assertFalse(data['is_manager'])

    def test_warehouses_lists_active_only(self):
        Warehouse.objects.create(name='Жабық қойма', is_active=False)
        self.client.force_login(self.manager)

        names = [w['name'] for w in self.client.get('/api/warehouses/').json()]

        self.assertEqual(names, ['Басты қойма', 'Екінші қойма'])


class SessionCsrfTests(ServiceMixin, TestCase):
    """Фронтенд API-ге сессиямен жүгінеді: өзгертетін сұраныс CSRF тақырыбынсыз өтпеуі тиіс."""

    @classmethod
    def setUpTestData(cls):
        create_fixtures(cls)
        cls.seller.warehouse = cls.warehouse
        cls.seller.save()

    def setUp(self):
        self.receive(10)
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.force_login(self.seller)
        self.client.get('/')  # csrftoken cookie
        self.token = self.client.cookies['csrftoken'].value
        self.sale = {'items': [{'barcode': '4870001112233', 'quantity': '1'}]}

    def test_post_without_csrf_header_is_rejected(self):
        response = self.client.post('/api/pos/sales/', self.sale, format='json')

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.stock(), 10)

    def test_post_with_csrf_header_succeeds(self):
        response = self.client.post('/api/pos/sales/', self.sale, format='json', HTTP_X_CSRFTOKEN=self.token)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.stock(), 9)
