from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        SELLER = 'SELLER', 'Сатушы'
        WAREHOUSEMAN = 'WAREHOUSEMAN', 'Қоймашы'
        MANAGER = 'MANAGER', 'Басқарушы'

    role = models.CharField('Рөлі', max_length=20, choices=Role.choices, default=Role.SELLER)
    # Кассир осы қоймадан сатады (POS API)
    warehouse = models.ForeignKey(
        'Warehouse', on_delete=models.PROTECT, related_name='staff',
        null=True, blank=True, verbose_name='Қоймасы',
    )

    class Meta:
        verbose_name = 'Қызметкер'
        verbose_name_plural = 'Қызметкерлер'

    def __str__(self):
        return f'{self.get_full_name() or self.username} ({self.get_role_display()})'

    @property
    def is_seller(self):
        return self.role == self.Role.SELLER

    @property
    def is_warehouseman(self):
        return self.role == self.Role.WAREHOUSEMAN

    @property
    def is_manager(self):
        return self.role == self.Role.MANAGER


class Product(models.Model):
    class Unit(models.TextChoices):
        PIECE = 'PCS', 'дана'
        KILOGRAM = 'KG', 'кг'
        LITER = 'L', 'литр'
        METER = 'M', 'метр'
        PACK = 'PACK', 'қаптама'

    name = models.CharField('Атауы', max_length=255)
    sku = models.CharField('Артикулы', max_length=64, unique=True)
    barcode = models.CharField('Штрихкоды', max_length=64, unique=True, null=True, blank=True)
    category = models.CharField('Санаты', max_length=128, blank=True)
    unit = models.CharField('Өлшем бірлігі', max_length=10, choices=Unit.choices, default=Unit.PIECE)
    sale_price = models.DecimalField(
        'Сату бағасы', max_digits=12, decimal_places=2, validators=[MinValueValidator(0)]
    )
    is_active = models.BooleanField('Белсенді', default=True)

    class Meta:
        verbose_name = 'Тауар'
        verbose_name_plural = 'Тауарлар'
        ordering = ['name']

    def __str__(self):
        return f'{self.name} [{self.sku}]'


class Supplier(models.Model):
    name = models.CharField('Атауы', max_length=255)
    contact_person = models.CharField('Байланыс тұлғасы', max_length=255, blank=True)
    phone = models.CharField('Телефоны', max_length=32, blank=True)
    email = models.EmailField('Email', blank=True)
    address = models.CharField('Мекенжайы', max_length=255, blank=True)
    bin = models.CharField('БСН/ЖСН', max_length=12, blank=True)
    bank_account = models.CharField('ИИК (шот)', max_length=34, blank=True)
    bank_name = models.CharField('Банк', max_length=255, blank=True)
    bik = models.CharField('БСК', max_length=11, blank=True)

    class Meta:
        verbose_name = 'Жеткізуші'
        verbose_name_plural = 'Жеткізушілер'
        ordering = ['name']

    def __str__(self):
        return self.name


class Warehouse(models.Model):
    name = models.CharField('Атауы', max_length=255, unique=True)
    address = models.CharField('Мекенжайы', max_length=255, blank=True)
    is_active = models.BooleanField('Белсенді', default=True)

    class Meta:
        verbose_name = 'Қойма'
        verbose_name_plural = 'Қоймалар'
        ordering = ['name']

    def __str__(self):
        return self.name


class Document(models.Model):
    class Type(models.TextChoices):
        SALE = 'SALE', 'Сату'
        SALE_RETURN = 'SALE_RETURN', 'Сатушылық қайтару'
        RECEIPT = 'RECEIPT', 'Жеткізушіден қабылдау'
        SUPPLIER_RETURN = 'SUPPLIER_RETURN', 'Жеткізушіге қайтару'
        WRITE_OFF = 'WRITE_OFF', 'Есептен шығару'
        INVENTORY = 'INVENTORY', 'Түгендеу'

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Жоба'
        POSTED = 'POSTED', 'Өткізілді'
        CANCELED = 'CANCELED', 'Күші жойылды'

    number = models.CharField('Нөмірі', max_length=32, unique=True)
    doc_type = models.CharField('Түрі', max_length=20, choices=Type.choices)
    status = models.CharField('Мәртебесі', max_length=10, choices=Status.choices, default=Status.DRAFT)
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name='documents', verbose_name='Қойма'
    )
    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, related_name='documents',
        null=True, blank=True, verbose_name='Жеткізуші',
    )
    responsible = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='documents', verbose_name='Жауапты қызметкер',
    )
    # Қайтару құжаттары үшін — қай құжаттан қайтарылып жатқаны (сату немесе жеткізушіден қабылдау)
    base_document = models.ForeignKey(
        'self', on_delete=models.PROTECT, related_name='returns',
        null=True, blank=True, limit_choices_to={'doc_type__in': ['SALE', 'RECEIPT']},
        verbose_name='Негіз құжат',
    )
    # Есептен шығаруда міндетті (неге шығарылды), басқа құжаттарда — қосымша түсініктеме
    comment = models.CharField('Себебі / түсініктеме', max_length=500, blank=True)
    date =models.DateTimeField('Датасы', auto_now_add=True)
    posted_at = models.DateTimeField('Өткізілген уақыты', null=True, blank=True)
    canceled_at = models.DateTimeField('Күшін жою уақыты', null=True, blank=True)
    canceled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='canceled_documents',
        null=True, blank=True, verbose_name='Күшін жойған',
    )
    # Өткізілген құжаттың күшін жойғанда міндетті (cancel_document)
    cancel_reason = models.CharField('Күшін жою себебі', max_length=500, blank=True)

    class Meta:
        verbose_name = 'Құжат'
        verbose_name_plural = 'Құжаттар'
        ordering = ['-date']
        indexes = [models.Index(fields=['doc_type', 'status'])]

    def __str__(self):
        return f'{self.get_doc_type_display()} №{self.number} ({self.get_status_display()})'

    def clean(self):
        # Форма (админ) деңгейіндегі ерте тексеру; өткізгенде сервис те тексереді
        if self.doc_type == self.Type.WRITE_OFF and not self.comment.strip():
            raise ValidationError({'comment': 'Есептен шығару себебі көрсетілуі тиіс.'})


class DocumentItem(models.Model):
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name='items', verbose_name='Құжат'
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name='document_items', verbose_name='Тауар'
    )
    quantity = models.DecimalField(
        'Саны', max_digits=12, decimal_places=3, validators=[MinValueValidator(0)]
    )
    price = models.DecimalField(
        'Бағасы', max_digits=12, decimal_places=2, validators=[MinValueValidator(0)]
    )
    # Түгендеу үшін: басталған сәттегі есептік қалдық және нақты сан енгізілді ме
    expected_quantity = models.DecimalField(
        'Есептік қалдық', max_digits=12, decimal_places=3, null=True, blank=True
    )
    is_counted = models.BooleanField('Саналды', default=True)

    class Meta:
        verbose_name = 'Құжат жолы'
        verbose_name_plural = 'Құжат жолдары'
        constraints = [
            models.UniqueConstraint(fields=['document', 'product'], name='unique_document_product'),
            models.CheckConstraint(condition=models.Q(quantity__gte=0), name='document_item_quantity_gte_0'),
            models.CheckConstraint(condition=models.Q(price__gte=0), name='document_item_price_gte_0'),
        ]

    def __str__(self):
        return f'{self.product} × {self.quantity}'

    @property
    def total(self):
        return self.quantity * self.price


class StockMovement(models.Model):
    """Тауар қозғалысының журналы. quantity оң — кіріс, теріс — шығыс."""

    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name='movements', verbose_name='Тауар'
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name='movements', verbose_name='Қойма'
    )
    quantity = models.DecimalField('Саны', max_digits=12, decimal_places=3)
    document = models.ForeignKey(
        Document, on_delete=models.PROTECT, related_name='movements', verbose_name='Құжат'
    )
    doc_type = models.CharField('Құжат типі', max_length=20, choices=Document.Type.choices)
    is_reversal = models.BooleanField('Кері операция', default=False)
    date = models.DateTimeField('Датасы', auto_now_add=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='stock_movements', verbose_name='Қызметкер',
    )

    class Meta:
        verbose_name = 'Тауар қозғалысы'
        verbose_name_plural = 'Тауар қозғалыстары'
        ordering = ['-date']
        indexes = [models.Index(fields=['product', 'warehouse'])]

    def __str__(self):
        sign = '+' if self.quantity > 0 else ''
        return f'{self.product} @ {self.warehouse}: {sign}{self.quantity} ({self.get_doc_type_display()})'


class Stock(models.Model):
    """Тауардың қоймадағы ағымдағы қалдығы. Өзгерткенде select_for_update() арқылы бұғатталады."""

    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name='stocks', verbose_name='Тауар'
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name='stocks', verbose_name='Қойма'
    )
    quantity = models.DecimalField('Қалдық', max_digits=12, decimal_places=3, default=0)
    updated_at = models.DateTimeField('Жаңартылған уақыты', auto_now=True)

    class Meta:
        verbose_name = 'Қалдық'
        verbose_name_plural = 'Қалдықтар'
        ordering = ['product__name']
        constraints = [
            models.UniqueConstraint(fields=['product', 'warehouse'], name='unique_stock_product_warehouse'),
            models.CheckConstraint(condition=models.Q(quantity__gte=0), name='stock_quantity_gte_0'),
        ]

    def __str__(self):
        return f'{self.product} @ {self.warehouse}: {self.quantity}'
