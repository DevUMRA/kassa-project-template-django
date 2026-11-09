"""Есеп жауаптарының құрылымы — тек OpenAPI құжаттамасы үшін (есептердің өзі inventory/reports.py-да dict қайтарады)."""
from rest_framework import serializers

from ..models import Document


def quantity(**kwargs):
    return serializers.DecimalField(max_digits=16, decimal_places=3, **kwargs)


def amount(**kwargs):
    return serializers.DecimalField(max_digits=18, decimal_places=2, **kwargs)


class PeriodMixin(serializers.Serializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()


# --- Қалдықтар ---

class StockRowSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    product_name = serializers.CharField()
    sku = serializers.CharField()
    barcode = serializers.CharField(allow_null=True)
    unit = serializers.CharField(help_text='Өлшем бірлігі (дана, кг, …)')
    warehouse_id = serializers.IntegerField()
    warehouse = serializers.CharField()
    quantity = quantity()
    sale_price = amount()
    value = amount(help_text='quantity × sale_price')


class StockTotalsSerializer(serializers.Serializer):
    positions = serializers.IntegerField()
    value = amount()


class StockReportSerializer(serializers.Serializer):
    rows = StockRowSerializer(many=True)
    totals = StockTotalsSerializer()


# --- Қозғалыс журналы ---

class MovementRowSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    date = serializers.DateTimeField()
    product_id = serializers.IntegerField()
    product_name = serializers.CharField()
    warehouse = serializers.CharField()
    quantity = quantity(help_text='Оң — кіріс, теріс — шығыс')
    doc_type = serializers.ChoiceField(choices=Document.Type.choices)
    doc_type_display = serializers.CharField()
    document_number = serializers.CharField()
    comment = serializers.CharField(help_text='Құжаттың себебі/түсініктемесі (бос болуы мүмкін)')
    is_reversal = serializers.BooleanField(help_text='Күшін жоюдан туған кері жазба')
    cancel_reason = serializers.CharField(help_text='Кері жазбада — күшін жою себебі, әйтпесе бос')
    user = serializers.CharField()


class MovementPageSerializer(PeriodMixin):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = MovementRowSerializer(many=True)


# --- Айналым ведомосі ---

class TurnoverRowSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    product_name = serializers.CharField()
    sku = serializers.CharField()
    warehouse_id = serializers.IntegerField()
    warehouse = serializers.CharField()
    opening = quantity(help_text='Кезең басындағы қалдық')
    incoming = quantity()
    outgoing = quantity()
    closing = quantity(help_text='opening + incoming − outgoing')


class TurnoverTotalsSerializer(serializers.Serializer):
    opening = quantity()
    incoming = quantity()
    outgoing = quantity()
    closing = quantity()


class TurnoverReportSerializer(PeriodMixin):
    rows = TurnoverRowSerializer(many=True)
    totals = TurnoverTotalsSerializer()


# --- Сату есебі ---

class SalesRowSerializer(serializers.Serializer):
    day = serializers.DateField(required=False, help_text='group_by=day болғанда')
    product_id = serializers.IntegerField(required=False, help_text='group_by=product болғанда')
    product_name = serializers.CharField(required=False, help_text='group_by=product болғанда')
    sku = serializers.CharField(required=False, help_text='group_by=product болғанда')
    cashier = serializers.CharField(required=False, help_text='group_by=cashier болғанда')
    receipts = serializers.IntegerField(help_text='Чектер саны')
    sold_qty = quantity()
    sold_amount = amount()
    returns = serializers.IntegerField(help_text='Қайтарулар саны')
    returned_qty = quantity()
    returned_amount = amount()
    net_amount = amount(help_text='sold_amount − returned_amount')


class SalesTotalsSerializer(serializers.Serializer):
    receipts = serializers.IntegerField()
    returns = serializers.IntegerField()
    sold_amount = amount()
    returned_amount = amount()
    net_amount = amount()


class SalesReportSerializer(PeriodMixin):
    group_by = serializers.ChoiceField(choices=['day', 'product', 'cashier'])
    rows = SalesRowSerializer(many=True)
    totals = SalesTotalsSerializer()


# --- Түгендеу нәтижелері ---

class AuditLineSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    product_name = serializers.CharField()
    sku = serializers.CharField()
    difference = quantity(help_text='Теріс — кемшілік, оң — артық')
    price = amount()
    amount = amount()


class ShortageSurplusSerializer(serializers.Serializer):
    shortage_qty = quantity()
    shortage_amount = amount()
    surplus_qty = quantity()
    surplus_amount = amount()


class AuditResultSerializer(ShortageSurplusSerializer):
    id = serializers.IntegerField()
    number = serializers.CharField()
    posted_at = serializers.DateTimeField()
    warehouse = serializers.CharField()
    comment = serializers.CharField(help_text='Түгендеу себебі / айырманың түсіндірмесі')
    positions = serializers.IntegerField(help_text='Түгенделген тауарлар саны')
    lines = AuditLineSerializer(many=True, help_text='Тек айырмасы бар тауарлар')


class InventoryTotalsSerializer(ShortageSurplusSerializer):
    audits = serializers.IntegerField()


class WriteOffRowSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    product_name = serializers.CharField()
    sku = serializers.CharField()
    documents = serializers.IntegerField()
    quantity = quantity()
    amount = amount()


class WriteOffDocumentSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    number = serializers.CharField()
    posted_at = serializers.DateTimeField()
    warehouse = serializers.CharField()
    reason = serializers.CharField(help_text='Есептен шығару себебі')
    quantity = quantity()
    amount = amount()


class WriteOffsSerializer(serializers.Serializer):
    rows = WriteOffRowSerializer(many=True, help_text='Тауар бойынша жиынтық')
    documents = WriteOffDocumentSerializer(many=True, help_text='Құжат бойынша, себебімен')
    total_quantity = quantity()
    total_amount = amount()


class InventoryReportSerializer(PeriodMixin):
    audits = AuditResultSerializer(many=True)
    totals = InventoryTotalsSerializer()
    write_offs = WriteOffsSerializer()
