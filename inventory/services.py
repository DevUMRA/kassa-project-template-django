import uuid
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Document, DocumentItem, Stock, StockMovement, User

# Құжат түрі бойынша қалдыққа әсер ету бағыты: +1 кіріс, -1 шығыс.
# INVENTORY мұнда жоқ — оның қозғалысы нақты сан мен есептік қалдықтың айырмасынан есептеледі.
DIRECTION = {
    Document.Type.RECEIPT: 1,
    Document.Type.SALE_RETURN: 1,
    Document.Type.SALE: -1,
    Document.Type.SUPPLIER_RETURN: -1,
    Document.Type.WRITE_OFF: -1,
}

# Қай рөл қай құжатты өткізе алады. MANAGER мен суперпайдаланушыға бәрі рұқсат.
ROLE_DOC_TYPES = {
    User.Role.SELLER: {Document.Type.SALE, Document.Type.SALE_RETURN},
    User.Role.WAREHOUSEMAN: {
        Document.Type.RECEIPT,
        Document.Type.SUPPLIER_RETURN,
        Document.Type.WRITE_OFF,
    },
}

SUPPLIER_DOC_TYPES = {Document.Type.RECEIPT, Document.Type.SUPPLIER_RETURN}

# Қайтару құжаты → оның негіз құжатының түрі
RETURN_BASE_TYPES = {
    Document.Type.SALE_RETURN: Document.Type.SALE,
    Document.Type.SUPPLIER_RETURN: Document.Type.RECEIPT,
}


def _is_manager(user):
    return user.is_superuser or user.role == User.Role.MANAGER


def _check_can_handle(user, doc_type):
    if _is_manager(user):
        return
    if doc_type not in ROLE_DOC_TYPES.get(user.role, set()):
        raise PermissionDenied(
            f'«{user.get_role_display()}» рөлі «{Document.Type(doc_type).label}» құжатымен жұмыс істей алмайды.'
        )


def _lock_document(document):
    """Құжатты қайта оқып, жолын бұғаттайды — бір құжатты қатар екі рет өткізуден қорғайды."""
    # of=('self',): PostgreSQL nullable supplier (LEFT JOIN) жолын FOR UPDATE-пен бұғаттай алмайды
    return (
        Document.objects.select_for_update(of=('self',))
        .select_related('warehouse', 'supplier')
        .get(pk=document.pk)
    )


def _lock_stocks(warehouse, product_ids):
    """Қалдық жолдарын product_id ретімен бұғаттайды (тұрақты рет deadlock-тың алдын алады)."""
    product_ids = sorted(set(product_ids))
    for product_id in product_ids:
        Stock.objects.get_or_create(product_id=product_id, warehouse=warehouse)
    stocks = (
        Stock.objects.select_for_update()
        .select_related('product')
        .filter(warehouse=warehouse, product_id__in=product_ids)
        .order_by('product_id')
    )
    return {stock.product_id: stock for stock in stocks}


def _check_inventory_lock(document, product_ids):
    """Түгендеу жүріп жатқан (DRAFT) тауарлар бойынша қозғалысқа тыйым салады."""
    locked = (
        DocumentItem.objects.filter(
            document__doc_type=Document.Type.INVENTORY,
            document__status=Document.Status.DRAFT,
            document__warehouse=document.warehouse,
            product_id__in=product_ids,
        )
        .exclude(document=document)
        .select_related('product', 'document')
    )
    errors = [
        f'«{item.product}» тауары №{item.document.number} түгендеуінде бұғатталған.'
        for item in locked
    ]
    if errors:
        raise ValidationError(errors)


def _validate_for_posting(document, items):
    errors = []
    if not items:
        errors.append('Құжатта бірде-бір тауар жоқ.')
    if not document.warehouse.is_active:
        errors.append(f'«{document.warehouse}» қоймасы белсенді емес.')
    if document.doc_type in SUPPLIER_DOC_TYPES and document.supplier_id is None:
        errors.append('Бұл құжат түрі үшін жеткізуші көрсетілуі тиіс.')
    for item in items:
        if not item.product.is_active:
            errors.append(f'«{item.product}» тауары белсенді емес.')
        # Түгендеуде нақты сан 0 болуы мүмкін, басқа құжаттарда — тек оң сан
        if document.doc_type != Document.Type.INVENTORY and item.quantity <= 0:
            errors.append(f'«{item.product}» тауарының саны нөлден үлкен болуы тиіс.')
        if document.doc_type == Document.Type.INVENTORY and not item.is_counted:
            errors.append(f'«{item.product}» тауары әлі саналмаған — нақты санын енгізіңіз.')
    if document.doc_type == Document.Type.WRITE_OFF and not document.comment.strip():
        errors.append('Есептен шығару себебі көрсетілуі тиіс (бүлінген, мерзімі өткен, …).')
    if document.base_document_id is not None and document.doc_type not in RETURN_BASE_TYPES:
        errors.append('Негіз құжат тек қайтару құжаттарында көрсетіледі.')
    if errors:
        raise ValidationError(errors)


def _validate_return(document, items):
    """Қайтару өткізілген негіз құжатқа (сату/қабылдау) байлануы және ондағы саннан аспауы тиіс."""
    base_type = RETURN_BASE_TYPES[document.doc_type]
    if document.base_document_id is None:
        raise ValidationError(f'Қайтаруда негіз құжат («{base_type.label}») көрсетілуі тиіс.')

    # Негіз құжатты бұғаттаймыз: оған қатар екі қайтару өткізілсе де, ондағы саннан асып кетпейді
    base = Document.objects.select_for_update(of=('self',)).get(pk=document.base_document_id)

    if base.doc_type != base_type:
        raise ValidationError(f'№{base.number} құжаты «{base_type.label}» емес.')

    errors = []
    if base.status != Document.Status.POSTED:
        errors.append(
            f'№{base.number} құжаты «{base.get_status_display()}» мәртебесінде — тек өткізілген құжаттан қайтаруға болады.'
        )
    if base.warehouse_id != document.warehouse_id:
        errors.append(f'Қайтару №{base.number} құжатының «{base.warehouse}» қоймасынан жасалуы тиіс.')
    if base.supplier_id != document.supplier_id:
        errors.append(f'Қайтару №{base.number} құжатының жеткізушісіне («{base.supplier}») жасалуы тиіс.')
    if errors:
        raise ValidationError(errors)

    based = {item.product_id: item.quantity for item in base.items.all()}
    returned = {}
    for item in DocumentItem.objects.filter(
        document__base_document=base,
        document__doc_type=document.doc_type,
        document__status=Document.Status.POSTED,
    ):
        returned[item.product_id] = returned.get(item.product_id, Decimal(0)) + item.quantity

    for item in items:
        if item.product_id not in based:
            errors.append(f'«{item.product}» тауары №{base.number} құжатында жоқ.')
            continue
        available = based[item.product_id] - returned.get(item.product_id, Decimal(0))
        if item.quantity > available:
            errors.append(
                f'«{item.product}»: №{base.number} құжатында {based[item.product_id]}, '
                f'бұрын қайтарылғаны {returned.get(item.product_id, Decimal(0))}, '
                f'қайтаруға болатыны {available}, сұралғаны {item.quantity}.'
            )
    if errors:
        raise ValidationError(errors)


def _apply_movements(document, stocks, deltas, user, is_reversal):
    """Қалдықты өзгертіп, әр өзгерісті StockMovement журналына жазады. Теріс қалдыққа жол бермейді."""
    shortages = [
        f'«{stocks[product_id].product}»: қолжетімді {stocks[product_id].quantity}, қажет {-delta}.'
        for product_id, delta in deltas.items()
        if stocks[product_id].quantity + delta < 0
    ]
    if shortages:
        raise ValidationError(['Қалдық жеткіліксіз:'] + shortages)

    movements = []
    for product_id, delta in deltas.items():
        if delta == 0:
            continue
        stock = stocks[product_id]
        stock.quantity += delta
        stock.save(update_fields=['quantity', 'updated_at'])
        movements.append(StockMovement(
            product_id=product_id,
            warehouse=document.warehouse,
            quantity=delta,
            document=document,
            doc_type=document.doc_type,
            is_reversal=is_reversal,
            user=user,
        ))
    StockMovement.objects.bulk_create(movements)


@transaction.atomic
def post_document(document, user, comment=None):
    """DRAFT құжатты өткізеді: қалдықты бір-ақ рет өзгертеді және журналға жазады.

    comment берілсе — өткізумен бірге сақталады (мысалы, түгендеуді бекіткенде айырманың түсіндірмесі).
    """
    document = _lock_document(document)
    _check_can_handle(user, document.doc_type)

    if document.status != Document.Status.DRAFT:
        raise ValidationError(
            f'№{document.number} құжаты «{document.get_status_display()}» мәртебесінде — тек жобаны өткізуге болады.'
        )
    if comment is not None:
        document.comment = comment.strip()

    items = list(document.items.select_related('product'))
    _validate_for_posting(document, items)
    if document.doc_type in RETURN_BASE_TYPES:
        _validate_return(document, items)

    product_ids = [item.product_id for item in items]
    stocks = _lock_stocks(document.warehouse, product_ids)
    # Бұғат қалдық бұғатталғаннан КЕЙІН тексеріледі: start_inventory де сол жолдарды бұғаттайды,
    # сондықтан қатар басталған түгендеу міндетті түрде көрінеді (READ COMMITTED)
    if document.doc_type != Document.Type.INVENTORY:
        _check_inventory_lock(document, product_ids)

    if document.doc_type == Document.Type.INVENTORY:
        # Түгендеу: құжаттағы сан — нақты саналған қалдық, қозғалыс — айырма
        deltas = {item.product_id: item.quantity - stocks[item.product_id].quantity for item in items}
    else:
        direction = DIRECTION[document.doc_type]
        deltas = {item.product_id: direction * item.quantity for item in items}

    _apply_movements(document, stocks, deltas, user, is_reversal=False)

    document.status = Document.Status.POSTED
    document.posted_at = timezone.now()
    document.save(update_fields=['status', 'posted_at', 'comment'])
    return document


@transaction.atomic
def cancel_document(document, user, reason=''):
    """Құжаттың күшін жояды. POSTED құжат ізсіз жойылмайды — кері қозғалыстар (reversal) жазылады.

    Кім жойғаны және себебі сақталады; өткізілген құжат үшін себеп міндетті.
    """
    document = _lock_document(document)
    reason = (reason or '').strip()

    if document.status == Document.Status.CANCELED:
        raise ValidationError(f'№{document.number} құжатының күші бұрын жойылған.')

    if document.status == Document.Status.POSTED:
        # Өткізілген құжатты кері қайтару — бақылау операциясы, тек Басқарушыға рұқсат
        if not _is_manager(user):
            raise PermissionDenied('Өткізілген құжаттың күшін тек Басқарушы жоя алады.')
        if not reason:
            raise ValidationError(f'№{document.number}: өткізілген құжаттың күшін жою себебі көрсетілуі тиіс.')

        # Өткізілген қайтаруы бар сатудың/қабылдаудың күшін жойсақ, қайтару негізсіз қалады
        active_returns = document.returns.filter(status=Document.Status.POSTED)
        if active_returns.exists():
            numbers = ', '.join(f'№{r.number}' for r in active_returns)
            raise ValidationError(
                f'№{document.number} құжатының өткізілген қайтарулары бар ({numbers}) — алдымен солардың күшін жойыңыз.'
            )

        deltas = {}
        for movement in document.movements.filter(is_reversal=False):
            deltas[movement.product_id] = deltas.get(movement.product_id, Decimal(0)) - movement.quantity

        stocks = _lock_stocks(document.warehouse, deltas.keys())
        # Түгендеу жүріп жатқан тауарлардың қалдығын кері операция да өзгертпеуі тиіс
        _check_inventory_lock(document, list(deltas))
        _apply_movements(document, stocks, deltas, user, is_reversal=True)
    else:
        # DRAFT қалдыққа әсер етпеген — тек мәртебесі өзгереді
        _check_can_handle(user, document.doc_type)

    document.status = Document.Status.CANCELED
    document.canceled_at = timezone.now()
    document.canceled_by = user
    document.cancel_reason = reason
    document.save(update_fields=['status', 'canceled_at', 'canceled_by', 'cancel_reason'])
    return document


# --- POS (касса) ---

NUMBER_PREFIXES = {
    Document.Type.SALE: 'SAL',
    Document.Type.SALE_RETURN: 'SRT',
    Document.Type.INVENTORY: 'INV',
    Document.Type.WRITE_OFF: 'WOF',
    Document.Type.RECEIPT: 'RCV',
    Document.Type.SUPPLIER_RETURN: 'RTS',
}


def _user_warehouse(user):
    if user.warehouse_id is None:
        raise ValidationError('Қызметкерге қойма бекітілмеген — операция мүмкін емес.')
    return user.warehouse


def _merge_lines(lines):
    """[(product, quantity), ...] → {product: жалпы саны}; бір тауардың қайталанған жолдары қосылады."""
    merged = {}
    for product, quantity in lines:
        merged[product] = merged.get(product, Decimal(0)) + quantity
    return merged


def _merge_priced_lines(lines):
    """[(product, quantity, price), ...] → {product: {quantity, price}}. Бір тауар әртүрлі бағамен келсе — қате."""
    merged = {}
    for product, quantity, price in lines:
        line = merged.setdefault(product, {'quantity': Decimal(0), 'price': price})
        if line['price'] != price:
            raise ValidationError(f'«{product}» тауары әртүрлі бағамен екі рет көрсетілген ({line["price"]} және {price}).')
        line['quantity'] += quantity
    return merged


def _create_document(doc_type, user, warehouse, items, base=None, comment='', supplier=None):
    """items — {product: {DocumentItem өрістері}}."""
    document = Document.objects.create(
        # Уақытша бірегей нөмір, pk белгілі болған соң ауыстырылады
        number=f'TMP-{uuid.uuid4().hex[:24]}',
        doc_type=doc_type,
        warehouse=warehouse,
        supplier=supplier,
        responsible=user,
        base_document=base,
        comment=comment.strip(),
    )
    document.number = f'{NUMBER_PREFIXES[doc_type]}-{document.pk:06d}'
    document.save(update_fields=['number'])
    DocumentItem.objects.bulk_create(
        DocumentItem(document=document, product=product, **fields) for product, fields in items.items()
    )
    return document


@transaction.atomic
def create_sale(user, lines):
    """Кассалық сату: құжатты жасап, бірден өткізеді. Қате болса, ештеңе сақталмайды (жоба да қалмайды).

    lines — [(product, quantity), ...]; баға әрқашан тауардың ағымдағы сату бағасынан алынады.
    """
    warehouse = _user_warehouse(user)
    merged = _merge_lines(lines)
    document = _create_document(
        Document.Type.SALE, user, warehouse,
        {product: {'quantity': quantity, 'price': product.sale_price} for product, quantity in merged.items()},
    )
    return post_document(document, user)


@transaction.atomic
def create_sale_return(user, sale, lines, comment=''):
    """Сатудан қайтару: құжатты жасап, бірден өткізеді. Баға сатудағы бағадан алынады. comment — қайтару себебі."""
    warehouse = _user_warehouse(user)
    merged = _merge_lines(lines)
    sale_prices = dict(sale.items.values_list('product_id', 'price'))
    document = _create_document(
        Document.Type.SALE_RETURN, user, warehouse,
        # Сатуда жоқ тауардың бағасы 0 — оны бәрібір _validate_return қабылдамайды
        {
            product: {'quantity': quantity, 'price': sale_prices.get(product.pk, Decimal(0))}
            for product, quantity in merged.items()
        },
        base=sale,
        comment=comment,
    )
    return post_document(document, user)


# --- Қойма ---

@transaction.atomic
def create_write_off(user, lines, comment):
    """Есептен шығару: құжатты қызметкердің қоймасында жасап, бірден өткізеді. Себеп міндетті.

    Баға — тауардың сату бағасы (есептегі шығын сомасы). Қате болса, ештеңе сақталмайды.
    """
    _check_can_handle(user, Document.Type.WRITE_OFF)
    warehouse = _user_warehouse(user)
    merged = _merge_lines(lines)
    document = _create_document(
        Document.Type.WRITE_OFF, user, warehouse,
        {product: {'quantity': quantity, 'price': product.sale_price} for product, quantity in merged.items()},
        comment=comment,
    )
    return post_document(document, user)


@transaction.atomic
def create_receipt(user, supplier, lines, comment=''):
    """Жеткізушіден қабылдау: құжатты қызметкердің қоймасында жасап, бірден өткізеді.

    lines — [(product, quantity, price), ...]; price — сатып алу бағасы (жеткізушінің накладнойы бойынша).
    """
    _check_can_handle(user, Document.Type.RECEIPT)
    warehouse = _user_warehouse(user)
    document = _create_document(
        Document.Type.RECEIPT, user, warehouse, _merge_priced_lines(lines), comment=comment, supplier=supplier,
    )
    return post_document(document, user)


@transaction.atomic
def create_supplier_return(user, receipt, lines, comment=''):
    """Жеткізушіге қайтару: құжатты жасап, бірден өткізеді. Жеткізуші мен баға қабылдау құжатынан алынады."""
    _check_can_handle(user, Document.Type.SUPPLIER_RETURN)
    warehouse = _user_warehouse(user)
    merged = _merge_lines(lines)
    receipt_prices = dict(receipt.items.values_list('product_id', 'price'))
    document = _create_document(
        Document.Type.SUPPLIER_RETURN, user, warehouse,
        # Қабылдауда жоқ тауардың бағасы 0 — оны бәрібір _validate_return қабылдамайды
        {
            product: {'quantity': quantity, 'price': receipt_prices.get(product.pk, Decimal(0))}
            for product, quantity in merged.items()
        },
        base=receipt,
        comment=comment,
        supplier=receipt.supplier,
    )
    return post_document(document, user)


# --- Түгендеу ---

INVENTORY_ROLES = {User.Role.WAREHOUSEMAN, User.Role.MANAGER}


def _check_inventory_staff(user, document=None):
    """Түгендеуді бастау және санау: Қоймашы мен Басқарушы. Қоймашы тек өз қоймасында."""
    if not (user.is_superuser or user.role in INVENTORY_ROLES):
        raise PermissionDenied('Түгендеумен тек Қоймашы мен Басқарушы жұмыс істей алады.')
    if document is not None and not _is_manager(user) and document.warehouse_id != user.warehouse_id:
        raise PermissionDenied('Түгендеу басқа қоймада жүріп жатыр.')


@transaction.atomic
def start_inventory(user, products, comment=''):
    """Түгендеуді бастайды: DRAFT INVENTORY құжаты жасалады, сол сәттен тауарлар бойынша қозғалыс бұғатталады.

    Әр жолға есептік қалдық (expected_quantity) жазылады, нақты сан кейін record_counts арқылы енгізіледі.
    """
    _check_inventory_staff(user)
    warehouse = _user_warehouse(user)
    products = sorted(set(products), key=lambda product: product.pk)
    if not products:
        raise ValidationError('Түгенделетін тауарлар көрсетілмеген.')

    # Қалдықты бұғаттаймыз: қатар өтіп жатқан операциялар аяқталғанша күтеміз, кейінгілері бұғатты көреді
    stocks = _lock_stocks(warehouse, [product.pk for product in products])
    busy = DocumentItem.objects.filter(
        document__doc_type=Document.Type.INVENTORY,
        document__status=Document.Status.DRAFT,
        document__warehouse=warehouse,
        product__in=products,
    ).select_related('product', 'document')
    errors = [f'«{item.product}» тауары №{item.document.number} түгендеуінде тұр.' for item in busy]
    if errors:
        raise ValidationError(errors)

    return _create_document(
        Document.Type.INVENTORY, user, warehouse,
        {
            product: {
                'quantity': Decimal(0),
                'price': product.sale_price,
                'expected_quantity': stocks[product.pk].quantity,
                'is_counted': False,
            }
            for product in products
        },
        comment=comment,
    )


@transaction.atomic
def record_counts(user, document, lines):
    """Түгендеуге нақты саналған санды енгізеді. Қайта енгізілсе — соңғысы сақталады."""
    document = _lock_document(document)
    _check_inventory_staff(user, document)
    if document.doc_type != Document.Type.INVENTORY or document.status != Document.Status.DRAFT:
        raise ValidationError(f'№{document.number} — жүріп жатқан түгендеу емес.')

    items = {item.product_id: item for item in document.items.select_related('product')}
    errors = []
    for product, quantity in lines:
        item = items.get(product.pk)
        if item is None:
            errors.append(f'«{product}» тауары №{document.number} түгендеуінде жоқ.')
            continue
        item.quantity = quantity
        item.is_counted = True
    if errors:
        raise ValidationError(errors)
    DocumentItem.objects.bulk_update(items.values(), ['quantity', 'is_counted'])
    return document
