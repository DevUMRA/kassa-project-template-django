"""Есептер: тек оқу. Барлығы StockMovement журналы мен POSTED құжаттардан есептеледі.

Әр функция JSON-ға дайын dict қайтарады: сандар жол түрінде (саны — 3, сомасы — 2 ондық таңбамен).
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, F, Q, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from .models import Document, DocumentItem, Stock, StockMovement

QTY = DecimalField(max_digits=16, decimal_places=3)
MONEY = DecimalField(max_digits=18, decimal_places=2)
ZERO = Decimal(0)

T = Document.Type
S = Document.Status


def qty(value):
    return str((value or ZERO).quantize(Decimal('0.001')))


def money(value):
    return str((value or ZERO).quantize(Decimal('0.01')))


def period_bounds(date_from, date_to):
    """[date_from 00:00, date_to+1 00:00) — ағымдағы уақыт белдеуінде, екі шеті де күн бойы қамтылады."""
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(date_from, time.min), tz)
    end = timezone.make_aware(datetime.combine(date_to + timedelta(days=1), time.min), tz)
    return start, end


def _by_warehouse(queryset, warehouse_ids, field='warehouse_id'):
    return queryset if warehouse_ids is None else queryset.filter(**{f'{field}__in': warehouse_ids})


# --- 1. Қалдықтар ---

def stock_report(warehouse_ids=None, search=None, low=None):
    """Ағымдағы қалдық және оның сату бағасымен құны. low — осы саннан аз немесе тең қалдықтар ғана."""
    stocks = _by_warehouse(Stock.objects.select_related('product', 'warehouse'), warehouse_ids)
    if search:
        stocks = stocks.filter(
            Q(product__name__icontains=search) | Q(product__sku__icontains=search) | Q(product__barcode=search)
        )
    if low is not None:
        stocks = stocks.filter(quantity__lte=low)

    rows, total_value = [], ZERO
    for stock in stocks.order_by('product__name', 'warehouse__name'):
        value = stock.quantity * stock.product.sale_price
        total_value += value
        rows.append({
            'product_id': stock.product_id,
            'product_name': stock.product.name,
            'sku': stock.product.sku,
            'barcode': stock.product.barcode,
            'unit': stock.product.get_unit_display(),
            'warehouse_id': stock.warehouse_id,
            'warehouse': stock.warehouse.name,
            'quantity': qty(stock.quantity),
            'sale_price': money(stock.product.sale_price),
            'value': money(value),
        })
    return {'rows': rows, 'totals': {'positions': len(rows), 'value': money(total_value)}}


# --- 2. Қозғалыс тарихы ---

def movements_queryset(start, end, warehouse_ids=None, product_id=None, doc_type=None):
    """Кезең ішіндегі журнал жазбалары (жаңалары бірінші) — API беттеп береді."""
    movements = _by_warehouse(
        StockMovement.objects.filter(date__gte=start, date__lt=end)
        .select_related('product', 'warehouse', 'document', 'user'),
        warehouse_ids,
    )
    if product_id is not None:
        movements = movements.filter(product_id=product_id)
    if doc_type:
        movements = movements.filter(doc_type=doc_type)
    return movements.order_by('-date', '-id')


def movement_row(movement):
    return {
        'id': movement.id,
        'date': movement.date,
        'product_id': movement.product_id,
        'product_name': movement.product.name,
        'warehouse': movement.warehouse.name,
        'quantity': qty(movement.quantity),
        'doc_type': movement.doc_type,
        'doc_type_display': movement.get_doc_type_display(),
        'document_number': movement.document.number,
        'comment': movement.document.comment,
        'is_reversal': movement.is_reversal,
        # Кері жазба — неге күші жойылғаны (жойған қызметкер — user)
        'cancel_reason': movement.document.cancel_reason if movement.is_reversal else '',
        'user': movement.user.username,
    }


def turnover_report(start, end, warehouse_ids=None, product_id=None):
    """Айналым ведомосі: бастапқы қалдық + кіріс − шығыс = соңғы қалдық (тауар × қойма бойынша).

    Кері операциялар (күшін жою) бағыты бойынша кіріс немесе шығысқа түседі.
    """
    movements = _by_warehouse(StockMovement.objects.filter(date__lt=end), warehouse_ids)
    if product_id is not None:
        movements = movements.filter(product_id=product_id)

    in_period = Q(date__gte=start)
    grouped = (
        movements.values('product_id', 'product__name', 'product__sku', 'warehouse_id', 'warehouse__name')
        .annotate(
            opening=Coalesce(Sum('quantity', filter=Q(date__lt=start)), ZERO, output_field=QTY),
            incoming=Coalesce(Sum('quantity', filter=in_period & Q(quantity__gt=0)), ZERO, output_field=QTY),
            outgoing=Coalesce(Sum('quantity', filter=in_period & Q(quantity__lt=0)), ZERO, output_field=QTY),
        )
        .order_by('product__name', 'warehouse__name')
    )

    rows = []
    totals = {'opening': ZERO, 'incoming': ZERO, 'outgoing': ZERO, 'closing': ZERO}
    for row in grouped:
        outgoing = -row['outgoing']
        closing = row['opening'] + row['incoming'] - outgoing
        if not (row['opening'] or row['incoming'] or outgoing):
            continue
        for key, value in (('opening', row['opening']), ('incoming', row['incoming']),
                           ('outgoing', outgoing), ('closing', closing)):
            totals[key] += value
        rows.append({
            'product_id': row['product_id'],
            'product_name': row['product__name'],
            'sku': row['product__sku'],
            'warehouse_id': row['warehouse_id'],
            'warehouse': row['warehouse__name'],
            'opening': qty(row['opening']),
            'incoming': qty(row['incoming']),
            'outgoing': qty(outgoing),
            'closing': qty(closing),
        })
    return {'rows': rows, 'totals': {key: qty(value) for key, value in totals.items()}}


# --- 3. Сату есебі ---

SALES_GROUPS = {
    'day': ('day',),
    'product': ('product_id', 'product__name', 'product__sku'),
    'cashier': ('document__responsible__username',),
}


def sales_report(start, end, warehouse_ids=None, group_by='day'):
    """Өткізілген сатулар мен сатушылық қайтарулар (posted_at бойынша). Түсім = сату − қайтару.

    Күші жойылған сатулар мен қайтарулар есепке кірмейді.
    """
    items = _by_warehouse(
        DocumentItem.objects.filter(
            document__status=S.POSTED,
            document__doc_type__in=(T.SALE, T.SALE_RETURN),
            document__posted_at__gte=start,
            document__posted_at__lt=end,
        ),
        warehouse_ids,
        field='document__warehouse_id',
    )
    if group_by == 'day':
        items = items.annotate(day=TruncDate('document__posted_at'))

    is_sale, is_return = Q(document__doc_type=T.SALE), Q(document__doc_type=T.SALE_RETURN)
    amount = F('quantity') * F('price')
    grouped = (
        items.values(*SALES_GROUPS[group_by])
        .annotate(
            receipts=Count('document', filter=is_sale, distinct=True),
            sold_qty=Coalesce(Sum('quantity', filter=is_sale), ZERO, output_field=QTY),
            sold_amount=Coalesce(Sum(amount, filter=is_sale, output_field=MONEY), ZERO, output_field=MONEY),
            returns=Count('document', filter=is_return, distinct=True),
            returned_qty=Coalesce(Sum('quantity', filter=is_return), ZERO, output_field=QTY),
            returned_amount=Coalesce(Sum(amount, filter=is_return, output_field=MONEY), ZERO, output_field=MONEY),
        )
        .order_by(*SALES_GROUPS[group_by])
    )

    rows = []
    totals = {'receipts': 0, 'returns': 0, 'sold_amount': ZERO, 'returned_amount': ZERO}
    for row in grouped:
        net = row['sold_amount'] - row['returned_amount']
        for key in totals:
            totals[key] += row[key]
        if group_by == 'day':
            key = {'day': row['day']}
        elif group_by == 'product':
            key = {'product_id': row['product_id'], 'product_name': row['product__name'], 'sku': row['product__sku']}
        else:
            key = {'cashier': row['document__responsible__username']}
        rows.append({
            **key,
            'receipts': row['receipts'],
            'sold_qty': qty(row['sold_qty']),
            'sold_amount': money(row['sold_amount']),
            'returns': row['returns'],
            'returned_qty': qty(row['returned_qty']),
            'returned_amount': money(row['returned_amount']),
            'net_amount': money(net),
        })
    return {
        'group_by': group_by,
        'rows': rows,
        'totals': {
            'receipts': totals['receipts'],
            'returns': totals['returns'],
            'sold_amount': money(totals['sold_amount']),
            'returned_amount': money(totals['returned_amount']),
            'net_amount': money(totals['sold_amount'] - totals['returned_amount']),
        },
    }


# --- 4. Түгендеу нәтижелері және есептен шығарулар ---

def inventory_report(start, end, warehouse_ids=None):
    """Бекітілген түгендеулердің кемшілігі/артығы (журналдағы нақты айырма) және кезеңдегі есептен шығарулар.

    Сомасы құжат жолындағы бағамен: түгендеуде — сату бағасы, есептен шығаруда — құжаттағы баға.
    """
    audits = _by_warehouse(
        Document.objects.filter(doc_type=T.INVENTORY, status=S.POSTED, posted_at__gte=start, posted_at__lt=end)
        .select_related('warehouse'),
        warehouse_ids,
    ).order_by('posted_at')

    totals = {'shortage_qty': ZERO, 'shortage_amount': ZERO, 'surplus_qty': ZERO, 'surplus_amount': ZERO}
    audit_rows = []
    for audit in audits:
        prices = dict(audit.items.values_list('product_id', 'price'))
        lines, audit_totals = [], dict.fromkeys(totals, ZERO)
        for movement in audit.movements.filter(is_reversal=False).select_related('product').order_by('product__name'):
            amount = movement.quantity * prices.get(movement.product_id, ZERO)
            side = 'shortage' if movement.quantity < 0 else 'surplus'
            audit_totals[f'{side}_qty'] += abs(movement.quantity)
            audit_totals[f'{side}_amount'] += abs(amount)
            lines.append({
                'product_id': movement.product_id,
                'product_name': movement.product.name,
                'sku': movement.product.sku,
                'difference': qty(movement.quantity),
                'price': money(prices.get(movement.product_id)),
                'amount': money(amount),
            })
        for key, value in audit_totals.items():
            totals[key] += value
        audit_rows.append({
            'id': audit.id,
            'number': audit.number,
            'posted_at': audit.posted_at,
            'warehouse': audit.warehouse.name,
            'comment': audit.comment,
            'positions': audit.items.count(),
            'lines': lines,
            'shortage_qty': qty(audit_totals['shortage_qty']),
            'shortage_amount': money(audit_totals['shortage_amount']),
            'surplus_qty': qty(audit_totals['surplus_qty']),
            'surplus_amount': money(audit_totals['surplus_amount']),
        })

    write_off_items = _by_warehouse(
        DocumentItem.objects.filter(
            document__doc_type=T.WRITE_OFF, document__status=S.POSTED,
            document__posted_at__gte=start, document__posted_at__lt=end,
        ),
        warehouse_ids,
        field='document__warehouse_id',
    )
    # Әр есептен шығару құжаты өз себебімен
    write_off_documents = [{
        'id': row['document_id'],
        'number': row['document__number'],
        'posted_at': row['document__posted_at'],
        'warehouse': row['document__warehouse__name'],
        'reason': row['document__comment'],
        'quantity': qty(row['quantity_sum']),
        'amount': money(row['amount']),
    } for row in (
        write_off_items
        .values('document_id', 'document__number', 'document__posted_at',
                'document__warehouse__name', 'document__comment')
        .annotate(quantity_sum=Sum('quantity'), amount=Sum(F('quantity') * F('price'), output_field=MONEY))
        .order_by('document__posted_at', 'document_id')
    )]
    write_offs = (
        write_off_items
        .values('product_id', 'product__name', 'product__sku')
        .annotate(
            documents=Count('document', distinct=True),
            quantity_sum=Sum('quantity'),
            amount=Sum(F('quantity') * F('price'), output_field=MONEY),
        )
        .order_by('product__name')
    )
    write_off_rows = [{
        'product_id': row['product_id'],
        'product_name': row['product__name'],
        'sku': row['product__sku'],
        'documents': row['documents'],
        'quantity': qty(row['quantity_sum']),
        'amount': money(row['amount']),
    } for row in write_offs]

    return {
        'audits': audit_rows,
        'totals': {
            'audits': len(audit_rows),
            'shortage_qty': qty(totals['shortage_qty']),
            'shortage_amount': money(totals['shortage_amount']),
            'surplus_qty': qty(totals['surplus_qty']),
            'surplus_amount': money(totals['surplus_amount']),
        },
        'write_offs': {
            'rows': write_off_rows,
            'documents': write_off_documents,
            'total_quantity': qty(sum((row['quantity_sum'] for row in write_offs), ZERO)),
            'total_amount': money(sum((row['amount'] for row in write_offs), ZERO)),
        },
    }
