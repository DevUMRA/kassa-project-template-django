from decimal import Decimal

from drf_spectacular.utils import extend_schema_field, inline_serializer
from rest_framework import serializers

from ..models import Document, DocumentItem, Product, Supplier

AMOUNT = serializers.DecimalField(max_digits=18, decimal_places=2)
QUANTITY_OR_NULL = serializers.DecimalField(max_digits=16, decimal_places=3, allow_null=True)


class ProductSerializer(serializers.ModelSerializer):
    unit_display = serializers.CharField(source='get_unit_display', read_only=True)
    # Кассирдің қоймасындағы қолжетімді қалдық (view annotate етеді)
    available = serializers.DecimalField(max_digits=12, decimal_places=3, read_only=True)

    class Meta:
        model = Product
        fields = ('id', 'name', 'sku', 'barcode', 'category', 'unit', 'unit_display', 'sale_price', 'available')


class LineSerializer(serializers.Serializer):
    """Чек жолы: тауар product_id немесе barcode арқылы көрсетіледі. Бағаны клиент жібермейді."""

    product_id = serializers.IntegerField(required=False)
    barcode = serializers.CharField(required=False)
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal('0.001'))

    def validate(self, attrs):
        if ('product_id' in attrs) == ('barcode' in attrs):
            raise serializers.ValidationError('product_id немесе barcode — тек біреуі көрсетілуі тиіс.')
        if 'product_id' in attrs:
            lookup, value = {'pk': attrs['product_id']}, f'id={attrs["product_id"]}'
        else:
            lookup, value = {'barcode': attrs['barcode']}, f'штрихкод {attrs["barcode"]}'
        product = Product.objects.filter(is_active=True, **lookup).first()
        if product is None:
            raise serializers.ValidationError(f'Белсенді тауар табылмады ({value}).')
        return {'product': product, 'quantity': attrs['quantity']}


class LinesSerializer(serializers.Serializer):
    items = LineSerializer(many=True, allow_empty=False)

    def lines(self):
        return [(line['product'], line['quantity']) for line in self.validated_data['items']]


def comment_field(help_text):
    return serializers.CharField(required=False, allow_blank=True, max_length=500, default='', help_text=help_text)


class ReturnLinesSerializer(LinesSerializer):
    comment = comment_field('Қайтару себебі (ақаулы, сәйкес келмеді, …)')


class ReceiptLineSerializer(LineSerializer):
    price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal(0),
                                     help_text='Сатып алу бағасы (накладной бойынша)')

    def validate(self, attrs):
        price = attrs['price']
        return {**super().validate(attrs), 'price': price}


class ReceiptRequestSerializer(serializers.Serializer):
    supplier_id = serializers.PrimaryKeyRelatedField(queryset=Supplier.objects.all(), source='supplier')
    items = ReceiptLineSerializer(many=True, allow_empty=False)
    comment = comment_field('Түсініктеме (накладной нөмірі, …)')

    def lines(self):
        return [(line['product'], line['quantity'], line['price']) for line in self.validated_data['items']]


class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = ('id', 'name', 'bin', 'phone')


class WriteOffLinesSerializer(LinesSerializer):
    comment = serializers.CharField(max_length=500, help_text='Есептен шығару себебі (міндетті): бүлінген, мерзімі өткен, …')


class CancelSerializer(serializers.Serializer):
    reason = serializers.CharField(
        required=False, allow_blank=True, max_length=500, default='',
        help_text='Күшін жою себебі: өткізілген құжат үшін міндетті, жоба үшін — қосымша.',
    )


class ApproveSerializer(serializers.Serializer):
    comment = serializers.CharField(
        required=False, allow_blank=True, max_length=500,
        help_text='Айырманың түсіндірмесі. Берілмесе — бастағандағы түсініктеме сақталады.',
    )


class CountLineSerializer(LineSerializer):
    # Түгендеуде нақты сан 0 болуы мүмкін (тауар мүлде табылмады)
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal(0))


class CountsSerializer(LinesSerializer):
    items = CountLineSerializer(many=True, allow_empty=False)


class StartInventorySerializer(serializers.Serializer):
    """Түгенделетін тауарлар: product_ids тізімі немесе all=true (барлық белсенді тауарлар)."""

    product_ids = serializers.ListField(child=serializers.IntegerField(), required=False, allow_empty=False)
    all = serializers.BooleanField(required=False, default=False)
    comment = comment_field('Түгендеу себебі (жоспарлы, ауысым тапсыру, …)')

    def validate(self, attrs):
        if bool(attrs.get('product_ids')) == attrs['all']:
            raise serializers.ValidationError('product_ids немесе all=true — тек біреуі көрсетілуі тиіс.')
        if attrs['all']:
            return {'products': list(Product.objects.filter(is_active=True)), 'comment': attrs['comment']}
        ids = set(attrs['product_ids'])
        products = list(Product.objects.filter(is_active=True, pk__in=ids))
        missing = ids - {product.pk for product in products}
        if missing:
            raise serializers.ValidationError(f'Белсенді тауар табылмады: {sorted(missing)}.')
        return {'products': products, 'comment': attrs['comment']}


class InventoryItemSerializer(serializers.ModelSerializer):
    product_id = serializers.IntegerField(read_only=True)
    product_name = serializers.CharField(source='product.name', read_only=True)
    sku = serializers.CharField(source='product.sku', read_only=True)
    barcode = serializers.CharField(source='product.barcode', read_only=True)
    counted_quantity = serializers.SerializerMethodField()
    difference = serializers.SerializerMethodField()

    class Meta:
        model = DocumentItem
        fields = ('product_id', 'product_name', 'sku', 'barcode',
                  'expected_quantity', 'counted_quantity', 'is_counted', 'difference')

    @extend_schema_field(QUANTITY_OR_NULL)
    def get_counted_quantity(self, item):
        return str(item.quantity) if item.is_counted else None

    @extend_schema_field(QUANTITY_OR_NULL)
    def get_difference(self, item):
        """Нақты − есептік: теріс — кем шықты, оң — артық шықты. Саналмаса — null."""
        if not item.is_counted or item.expected_quantity is None:
            return None
        return str(item.quantity - item.expected_quantity)


def canceled_by_field():
    return serializers.CharField(source='canceled_by.username', read_only=True, allow_null=True,
                                 help_text='Күшін жойған қызметкер (жойылмаса — null)')


class InventorySerializer(serializers.ModelSerializer):
    warehouse = serializers.CharField(source='warehouse.name', read_only=True)
    responsible = serializers.CharField(source='responsible.username', read_only=True)
    canceled_by = canceled_by_field()
    items = InventoryItemSerializer(many=True, read_only=True)
    progress = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = ('id', 'number', 'status', 'warehouse', 'responsible', 'date', 'posted_at', 'canceled_at',
                  'canceled_by', 'cancel_reason', 'comment', 'progress', 'items')

    @extend_schema_field(inline_serializer('InventoryProgress', {
        'total': serializers.IntegerField(), 'counted': serializers.IntegerField(),
    }))
    def get_progress(self, document):
        items = document.items.all()
        return {'total': len(items), 'counted': sum(1 for item in items if item.is_counted)}


class DocumentItemSerializer(serializers.ModelSerializer):
    product_id = serializers.IntegerField(read_only=True)
    product_name = serializers.CharField(source='product.name', read_only=True)
    barcode = serializers.CharField(source='product.barcode', read_only=True)
    total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = DocumentItem
        fields = ('product_id', 'product_name', 'barcode', 'quantity', 'price', 'total')


class ReturnSerializer(serializers.ModelSerializer):
    items = DocumentItemSerializer(many=True, read_only=True)
    canceled_by = canceled_by_field()
    total = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = ('id', 'number', 'status', 'date', 'posted_at', 'canceled_at', 'canceled_by', 'cancel_reason',
                  'comment', 'items', 'total')

    @extend_schema_field(AMOUNT)
    def get_total(self, document):
        return str(sum((item.total for item in document.items.all()), Decimal('0.00')).quantize(Decimal('0.01')))


class WriteOffSerializer(ReturnSerializer):
    warehouse = serializers.CharField(source='warehouse.name', read_only=True)
    responsible = serializers.CharField(source='responsible.username', read_only=True)

    class Meta(ReturnSerializer.Meta):
        fields = ReturnSerializer.Meta.fields + ('warehouse', 'responsible')


class WithReturnsSerializer(ReturnSerializer):
    """Қайтаруға болатын құжат (сату, қабылдау): оның қайтарулары және әлі қайтаруға болатын саны."""

    warehouse = serializers.CharField(source='warehouse.name', read_only=True)
    returns = ReturnSerializer(many=True, read_only=True)
    returnable = serializers.SerializerMethodField()

    class Meta(ReturnSerializer.Meta):
        fields = ReturnSerializer.Meta.fields + ('warehouse', 'returns', 'returnable')

    @extend_schema_field({
        'type': 'object',
        'additionalProperties': {'type': 'string', 'format': 'decimal'},
        'example': {'4': '2.000'},
        'description': '{product_id: әлі қайтаруға болатын саны}',
    })
    def get_returnable(self, document):
        """Әр тауар бойынша әлі қайтаруға болатын сан: {product_id: саны}. Өткізілген қайтарулар шегеріледі."""
        if document.status != Document.Status.POSTED:
            return {}
        remaining = {item.product_id: item.quantity for item in document.items.all()}
        for doc_return in document.returns.all():
            if doc_return.status == Document.Status.POSTED:
                for item in doc_return.items.all():
                    remaining[item.product_id] -= item.quantity
        return {str(product_id): str(quantity) for product_id, quantity in remaining.items()}


class DocumentFilterSerializer(serializers.Serializer):
    """Құжаттар журналының сүзгілері (query params)."""

    doc_type = serializers.ChoiceField(choices=Document.Type.choices, required=False, allow_blank=True)
    status = serializers.ChoiceField(choices=Document.Status.choices, required=False, allow_blank=True)
    warehouse = serializers.IntegerField(required=False)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)

    def validate(self, attrs):
        if attrs.get('date_from') and attrs.get('date_to') and attrs['date_from'] > attrs['date_to']:
            raise serializers.ValidationError('date_from date_to-дан кейін болмауы тиіс.')
        return attrs


class DocumentLinkSerializer(serializers.ModelSerializer):
    class Meta:
        model = Document
        fields = ('id', 'number', 'doc_type', 'status')


class DocumentSerializer(ReturnSerializer):
    """Құжаттар журналы (Басқарушы): кез келген түрдегі құжат толық ақпаратымен."""

    doc_type_display = serializers.CharField(source='get_doc_type_display', read_only=True)
    warehouse = serializers.CharField(source='warehouse.name', read_only=True)
    supplier = serializers.CharField(source='supplier.name', read_only=True, allow_null=True)
    responsible = serializers.CharField(source='responsible.username', read_only=True)
    base_document = DocumentLinkSerializer(read_only=True, allow_null=True)
    returns = DocumentLinkSerializer(many=True, read_only=True)

    class Meta(ReturnSerializer.Meta):
        fields = ('id', 'number', 'doc_type', 'doc_type_display', 'status', 'warehouse', 'supplier', 'responsible',
                  'base_document', 'returns', 'date', 'posted_at', 'canceled_at', 'canceled_by', 'cancel_reason',
                  'comment', 'items', 'total')


class SaleSerializer(WithReturnsSerializer):
    cashier = serializers.CharField(source='responsible.username', read_only=True)

    class Meta(WithReturnsSerializer.Meta):
        fields = WithReturnsSerializer.Meta.fields + ('cashier',)


class ReceiptSerializer(WithReturnsSerializer):
    supplier_id = serializers.IntegerField(read_only=True)
    supplier = serializers.CharField(source='supplier.name', read_only=True)
    responsible = serializers.CharField(source='responsible.username', read_only=True)

    class Meta(WithReturnsSerializer.Meta):
        fields = WithReturnsSerializer.Meta.fields + ('supplier_id', 'supplier', 'responsible')
