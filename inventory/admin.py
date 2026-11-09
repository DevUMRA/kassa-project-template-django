from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.core.exceptions import PermissionDenied, ValidationError
from django.template.response import TemplateResponse

from .models import (
    Document,
    DocumentItem,
    Product,
    Stock,
    StockMovement,
    Supplier,
    User,
    Warehouse,
)
from .services import ROLE_DOC_TYPES, cancel_document, post_document

R = User.Role


def is_manager(user):
    return user.is_superuser or user.role == R.MANAGER


class RolePermissionsMixin:
    """Админдегі рұқсат Django model-perms бойынша емес, рөл бойынша (CLAUDE.md RBAC).

    Суперпайдаланушы мен Басқарушы — бәрі; қалған рөлдер view_roles (қарау) және edit_roles
    (қосу/өзгерту/өшіру) арқылы. Админге кіру үшін қызметкерде is_staff болуы тиіс.
    """

    view_roles = frozenset()
    edit_roles = frozenset()
    managers_can_edit = True

    @staticmethod
    def _is_staff(user):
        # Кіру беті (AnonymousUser-де role жоқ) және бұғатталған қызметкер үшін — ешқандай рұқсат жоқ
        return user.is_authenticated and user.is_active and user.is_staff

    def can_edit(self, request):
        user = request.user
        return self._is_staff(user) and (
            user.is_superuser
            or (user.role == R.MANAGER and self.managers_can_edit)
            or user.role in self.edit_roles
        )

    def has_module_permission(self, request):
        return self.has_view_permission(request)

    def has_view_permission(self, request, obj=None):
        user = request.user
        return self._is_staff(user) and (
            is_manager(user) or user.role in self.view_roles or user.role in self.edit_roles
        )

    def has_add_permission(self, request, *args):
        return self.can_edit(request)

    def has_change_permission(self, request, obj=None):
        return self.can_edit(request)

    def has_delete_permission(self, request, obj=None):
        return self.can_edit(request)


class OwnWarehouseMixin:
    """Басқарушы емес қызметкер тек өз қоймасының жазбаларын көреді."""

    warehouse_field = 'warehouse'

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if is_manager(request.user):
            return queryset
        if request.user.warehouse_id is None:
            return queryset.none()
        return queryset.filter(**{f'{self.warehouse_field}_id': request.user.warehouse_id})


@admin.register(User)
class UserAdmin(RolePermissionsMixin, BaseUserAdmin):
    # Қызметкерлерді тек суперпайдаланушы өзгертеді: әйтпесе Басқарушы өзіне кез келген құқық бере алар еді
    managers_can_edit = False

    list_display = ('username', 'first_name', 'last_name', 'role', 'warehouse', 'is_active', 'is_staff')
    list_filter = ('role', 'warehouse', 'is_active', 'is_staff', 'is_superuser')
    fieldsets = BaseUserAdmin.fieldsets + (('Рөлі', {'fields': ('role', 'warehouse')}),)
    add_fieldsets = BaseUserAdmin.add_fieldsets + (('Рөлі', {'fields': ('role', 'warehouse')}),)


@admin.register(Product)
class ProductAdmin(RolePermissionsMixin, admin.ModelAdmin):
    view_roles = frozenset({R.WAREHOUSEMAN, R.SELLER})

    list_display = ('name', 'sku', 'barcode', 'category', 'unit', 'sale_price', 'is_active')
    list_filter = ('is_active', 'category', 'unit')
    search_fields = ('name', 'sku', 'barcode')
    list_editable = ('is_active',)


@admin.register(Supplier)
class SupplierAdmin(RolePermissionsMixin, admin.ModelAdmin):
    view_roles = frozenset({R.WAREHOUSEMAN})

    list_display = ('name', 'contact_person', 'phone', 'email', 'bin')
    search_fields = ('name', 'bin', 'phone', 'email')
    fieldsets = (
        (None, {'fields': ('name',)}),
        ('Байланыс деректері', {'fields': ('contact_person', 'phone', 'email', 'address')}),
        ('Реквизиттері', {'fields': ('bin', 'bank_account', 'bank_name', 'bik')}),
    )


@admin.register(Warehouse)
class WarehouseAdmin(RolePermissionsMixin, admin.ModelAdmin):
    view_roles = frozenset({R.WAREHOUSEMAN})

    list_display = ('name', 'address', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name', 'address')


class DocumentItemInline(admin.TabularInline):
    model = DocumentItem
    extra = 1
    autocomplete_fields = ('product',)
    readonly_fields = ('expected_quantity',)
    exclude = ('is_counted',)

    # Құжат жолдарының рұқсаты — құжаттың өзінікі (өткізілген/күші жойылған құжат өзгертілмейді)
    def _document_admin(self):
        return self.admin_site._registry[Document]

    def has_view_permission(self, request, obj=None):
        return self._document_admin().has_view_permission(request, obj)

    def has_add_permission(self, request, obj=None):
        return self.has_change_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        document_admin = self._document_admin()
        if obj is None:
            return document_admin.has_add_permission(request)
        return document_admin.has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self.has_change_permission(request, obj)


@admin.register(Document)
class DocumentAdmin(OwnWarehouseMixin, RolePermissionsMixin, admin.ModelAdmin):
    """Басқарушы — барлық құжат. Қоймашы — өз қоймасы, DRAFT қабылдау/қайтару/шығаруды өңдейді.
    Сатушы — өз қоймасының сату/қайтаруларын тек қарайды (касса — POS API арқылы)."""

    view_roles = frozenset({R.SELLER})
    edit_roles = frozenset({R.WAREHOUSEMAN})

    list_display = ('number', 'doc_type', 'status', 'warehouse', 'supplier', 'responsible', 'date', 'short_comment')
    list_filter = ('doc_type', 'status', 'warehouse')
    search_fields = ('number', 'comment')
    date_hierarchy = 'date'
    inlines = (DocumentItemInline,)
    autocomplete_fields = ('supplier', 'base_document')
    actions = ('post_documents', 'cancel_documents')

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if request.user.role == R.SELLER and not request.user.is_superuser:
            queryset = queryset.filter(doc_type__in=ROLE_DOC_TYPES[R.SELLER])
        return queryset

    @admin.display(description='Себебі / түсініктеме', ordering='comment')
    def short_comment(self, obj):
        return obj.comment if len(obj.comment) <= 60 else obj.comment[:57] + '…'

    def _editable_types(self, request):
        return set(Document.Type) if is_manager(request.user) else ROLE_DOC_TYPES.get(request.user.role, set())

    def has_change_permission(self, request, obj=None):
        if not self.can_edit(request):
            return False
        if obj is None:
            return True
        # Мәртебе тек сервис арқылы өзгереді — өткізілген/күші жойылған құжат тек қаралады
        return obj.status == Document.Status.DRAFT and obj.doc_type in self._editable_types(request)

    def has_delete_permission(self, request, obj=None):
        return self.has_change_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not self.can_edit(request):
            return {}
        # Өткізілген құжаттың күшін тек Басқарушы жояды (сервис те тексереді)
        if not is_manager(request.user):
            actions.pop('cancel_documents', None)
        return actions

    def get_readonly_fields(self, request, obj=None):
        readonly = ['status', 'date', 'posted_at', 'canceled_at', 'canceled_by', 'cancel_reason']
        if not is_manager(request.user):
            # Жауапты — әрқашан құжатты жасаған қызметкердің өзі (save_model)
            readonly.append('responsible')
        return readonly

    def formfield_for_choice_field(self, db_field, request, **kwargs):
        if db_field.name == 'doc_type' and not is_manager(request.user):
            allowed = self._editable_types(request)
            kwargs['choices'] = [choice for choice in Document.Type.choices if choice[0] in allowed]
        return super().formfield_for_choice_field(db_field, request, **kwargs)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'warehouse' and not is_manager(request.user):
            kwargs['queryset'] = Warehouse.objects.filter(pk=request.user.warehouse_id)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if not is_manager(request.user):
            obj.responsible = request.user
        super().save_model(request, obj, form, change)

    def _run_service(self, request, queryset, service, success_text):
        for document in queryset.order_by('pk'):
            try:
                service(document, request.user)
            except ValidationError as e:
                self.message_user(request, f'№{document.number}: {" ".join(e.messages)}', messages.ERROR)
            except PermissionDenied as e:
                self.message_user(request, f'№{document.number}: {e}', messages.ERROR)
            else:
                self.message_user(request, f'№{document.number}: {success_text}', messages.SUCCESS)

    @admin.action(description='Таңдалған құжаттарды өткізу')
    def post_documents(self, request, queryset):
        self._run_service(request, queryset, post_document, 'өткізілді.')

    @admin.action(description='Таңдалған құжаттардың күшін жою')
    def cancel_documents(self, request, queryset):
        """Аралық бет: себепті сұрап, растағаннан кейін ғана күшін жояды."""
        has_posted = queryset.filter(status=Document.Status.POSTED).exists()
        form = CancelReasonForm(request.POST if 'apply' in request.POST else None, reason_required=has_posted)
        if form.is_bound and form.is_valid():
            reason = form.cleaned_data['reason']
            self._run_service(
                request, queryset, lambda document, user: cancel_document(document, user, reason), 'күші жойылды.'
            )
            return None
        return TemplateResponse(request, 'admin/inventory/document/cancel_confirmation.html', {
            **self.admin_site.each_context(request),
            'title': 'Құжаттардың күшін жою',
            'opts': self.model._meta,
            'documents': queryset.select_related('warehouse').order_by('pk'),
            'has_posted': has_posted,
            'form': form,
            'action_checkbox_name': ACTION_CHECKBOX_NAME,
        })


class CancelReasonForm(forms.Form):
    reason = forms.CharField(
        label='Күшін жою себебі', max_length=500,
        widget=forms.Textarea(attrs={'rows': 3, 'cols': 60}),
    )

    def __init__(self, *args, reason_required=True, **kwargs):
        super().__init__(*args, **kwargs)
        # Себеп өткізілген құжат үшін міндетті (сервис те тексереді), жоба үшін — қосымша
        self.fields['reason'].required = reason_required


class ReadOnlyAdmin(RolePermissionsMixin, admin.ModelAdmin):
    """Журнал, қалдық және құжат жолдарының тізімі — тек қарау. Өзгеріс тек құжат пен сервис арқылы."""

    def can_edit(self, request):
        return False


@admin.register(DocumentItem)
class DocumentItemAdmin(OwnWarehouseMixin, ReadOnlyAdmin):
    view_roles = frozenset({R.WAREHOUSEMAN})
    warehouse_field = 'document__warehouse'

    list_display = ('document', 'product', 'quantity', 'price', 'total')
    list_filter = ('document__doc_type', 'document__status')
    search_fields = ('document__number', 'product__name', 'product__sku')
    list_select_related = ('document', 'product')


@admin.register(StockMovement)
class StockMovementAdmin(OwnWarehouseMixin, ReadOnlyAdmin):
    view_roles = frozenset({R.WAREHOUSEMAN})

    list_display = ('date', 'product', 'warehouse', 'quantity', 'doc_type', 'document', 'is_reversal', 'user')
    list_filter = ('doc_type', 'is_reversal', 'warehouse')
    search_fields = ('product__name', 'product__sku', 'document__number')
    date_hierarchy = 'date'
    list_select_related = ('product', 'warehouse', 'document', 'user')


@admin.register(Stock)
class StockAdmin(OwnWarehouseMixin, ReadOnlyAdmin):
    view_roles = frozenset({R.WAREHOUSEMAN, R.SELLER})

    list_display = ('product', 'warehouse', 'quantity', 'updated_at')
    list_filter = ('warehouse',)
    search_fields = ('product__name', 'product__sku', 'product__barcode')
    list_select_related = ('product', 'warehouse')
