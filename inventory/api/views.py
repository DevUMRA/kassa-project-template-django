import logging
from decimal import Decimal

from django.db import DatabaseError, connection
from django.db.models import DecimalField, OuterRef, Prefetch, Subquery, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view, inline_serializer
from rest_framework import filters, mixins, serializers, status, viewsets
from rest_framework.decorators import action, api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from ..models import Document, DocumentItem, Product, Stock, Supplier, User, Warehouse
from ..reports import period_bounds
from ..services import (
    cancel_document,
    create_receipt,
    create_sale,
    create_sale_return,
    create_supplier_return,
    create_write_off,
    post_document,
    record_counts,
    start_inventory,
)
from .exceptions import ERROR_RESPONSES
from .permissions import IsCashier, IsInventoryStaff, IsManagerDocuments, IsWarehouseStaff
from .serializers import (
    ApproveSerializer,
    CancelSerializer,
    CountsSerializer,
    DocumentFilterSerializer,
    DocumentSerializer,
    InventorySerializer,
    LinesSerializer,
    ProductSerializer,
    ReceiptRequestSerializer,
    ReceiptSerializer,
    ReturnLinesSerializer,
    ReturnSerializer,
    SaleSerializer,
    StartInventorySerializer,
    SupplierSerializer,
    WriteOffLinesSerializer,
    WriteOffSerializer,
)

_items = Prefetch('items', queryset=DocumentItem.objects.select_related('product'))
_returns = Prefetch('returns', queryset=Document.objects.select_related('canceled_by').prefetch_related(_items))


@extend_schema(
    tags=['Қызмет'], summary='Ағымдағы пайдаланушы',
    description='Фронтенд үшін: рөл, қойма және қолжетімді бөлімдер.',
    responses={200: inline_serializer('Me', {
        'username': serializers.CharField(),
        'full_name': serializers.CharField(),
        'role': serializers.ChoiceField(choices=User.Role.choices),
        'role_display': serializers.CharField(),
        'is_manager': serializers.BooleanField(),
        'warehouse': inline_serializer('MeWarehouse', {
            'id': serializers.IntegerField(), 'name': serializers.CharField(),
        }, allow_null=True),
        'sections': serializers.ListField(
            child=serializers.ChoiceField(choices=['pos', 'warehouse', 'inventory', 'documents', 'reports'])
        ),
    })},
)
@api_view(['GET'])
def me(request):
    user = request.user
    manager = user.is_superuser or user.role == User.Role.MANAGER
    sections = []
    if manager or user.role == User.Role.SELLER:
        sections.append('pos')
    if manager or user.role == User.Role.WAREHOUSEMAN:
        sections += ['warehouse', 'inventory']
        if manager:
            sections.append('documents')
        sections.append('reports')
    return Response({
        'username': user.username,
        'full_name': user.get_full_name() or user.username,
        'role': user.role,
        'role_display': 'Әкімші' if user.is_superuser else user.get_role_display(),
        'is_manager': manager,
        'warehouse': {'id': user.warehouse_id, 'name': user.warehouse.name} if user.warehouse_id else None,
        'sections': sections,
    })


@extend_schema(
    tags=['Қызмет'], summary='Қоймалар тізімі',
    responses={200: inline_serializer('WarehouseItem', {
        'id': serializers.IntegerField(), 'name': serializers.CharField(),
    }, many=True)},
)
@api_view(['GET'])
def warehouses(request):
    return Response(list(Warehouse.objects.filter(is_active=True).order_by('name').values('id', 'name')))


@extend_schema(
    tags=['Қызмет'], summary='Денсаулық тексеруі',
    description='Аутентификациясыз. Мониторинг/load balancer үшін: қолданба мен PostgreSQL жауап бере ме.',
    responses={
        200: inline_serializer('Health', {'status': serializers.CharField(), 'database': serializers.CharField()}),
        503: inline_serializer('Unhealthy', {'status': serializers.CharField(), 'database': serializers.CharField()}),
    },
)
@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def health(request):
    try:
        connection.ensure_connection()
    except DatabaseError:
        logger.exception('Health check: дерекқорға қосылу мүмкін емес')
        return Response({'status': 'error', 'database': 'unavailable'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response({'status': 'ok', 'database': 'ok'})

ID = OpenApiParameter('id', OpenApiTypes.INT, OpenApiParameter.PATH)

logger = logging.getLogger(__name__)


def _schema_generation(view):
    """drf-spectacular схеманы нақты пайдаланушысыз жасайды — queryset-ке request.user керек емес."""
    return getattr(view, 'swagger_fake_view', False)


@extend_schema_view(
    list=extend_schema(summary='Тауарлар тізімі'),
    retrieve=extend_schema(summary='Тауар', parameters=[ID]),
)
@extend_schema(tags=['POS: тауарлар'])
class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    """Белсенді тауарлар және кассир қоймасындағы қолжетімді қалдық (`available`). Барлық рөлдер."""

    serializer_class = ProductSerializer
    filter_backends = (filters.SearchFilter,)
    search_fields = ('name', 'sku', '=barcode')

    def get_queryset(self):
        if _schema_generation(self):
            return Product.objects.none()
        available = Stock.objects.filter(
            product=OuterRef('pk'), warehouse_id=self.request.user.warehouse_id
        ).values('quantity')
        return (
            Product.objects.filter(is_active=True)
            .annotate(available=Coalesce(
                Subquery(available), Value(Decimal(0)),
                output_field=DecimalField(max_digits=12, decimal_places=3),
            ))
            .order_by('name')
        )

    @extend_schema(
        summary='Тауарды штрихкод бойынша табу (сканер)',
        parameters=[OpenApiParameter('barcode', OpenApiTypes.STR, OpenApiParameter.PATH)],
        responses={200: ProductSerializer, 404: None},
    )
    @action(detail=False, url_path=r'barcode/(?P<barcode>[^/]+)')
    def by_barcode(self, request, barcode=None):
        product = get_object_or_404(self.get_queryset(), barcode=barcode)
        return Response(self.get_serializer(product).data)


@extend_schema_view(
    list=extend_schema(summary='Сатулар (чектер) тізімі'),
    retrieve=extend_schema(summary='Чек', parameters=[ID]),
)
@extend_schema(tags=['POS: сату'])
class SaleViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                  viewsets.GenericViewSet):
    """Кассалық сатулар: Сатушы, Басқарушы. Сатушы өз қоймасының сатуларын көреді, Басқарушы — барлығын.

    `search` — чек нөмірі бойынша дәл іздеу.
    """

    serializer_class = SaleSerializer
    permission_classes = (IsCashier,)
    filter_backends = (filters.SearchFilter,)
    search_fields = ('=number',)

    def get_queryset(self):
        if _schema_generation(self):
            return Document.objects.none()
        queryset = (
            Document.objects.filter(doc_type=Document.Type.SALE)
            .select_related('warehouse', 'responsible', 'canceled_by')
            .prefetch_related(_items, _returns)
            .order_by('-date')
        )
        user = self.request.user
        if not (user.is_superuser or user.role == User.Role.MANAGER):
            queryset = queryset.filter(warehouse_id=user.warehouse_id)
        return queryset

    @extend_schema(
        summary='Сату (чек)',
        description=(
            'Сату құжатын жасап, бірден өткізеді — бір транзакцияда. Баға тауардың сату бағасынан алынады '
            '(клиент жібермейді), бір тауардың қайталанған жолдары қосылады. Қате болса ештеңе сақталмайды. '
            'Кассирге қойма бекітілген болуы тиіс.'
        ),
        request=LinesSerializer,
        responses={201: SaleSerializer, **ERROR_RESPONSES},
    )
    def create(self, request, *args, **kwargs):
        lines = LinesSerializer(data=request.data)
        lines.is_valid(raise_exception=True)
        sale = create_sale(request.user, lines.lines())
        return Response(self.get_serializer(self.get_queryset().get(pk=sale.pk)).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary='Сатудан қайтару',
        description=(
            'Сатушылық қайтару құжатын жасап, бірден өткізеді. Баға сатудағы бағадан алынады. '
            'Сатылған саннан (бұрынғы қайтаруларды шегергенде) артық қайтаруға болмайды.'
        ),
        parameters=[ID],
        request=ReturnLinesSerializer,
        responses={201: ReturnSerializer, **ERROR_RESPONSES},
    )
    @action(detail=True, methods=['post'])
    def returns(self, request, pk=None):
        sale = self.get_object()
        lines = ReturnLinesSerializer(data=request.data)
        lines.is_valid(raise_exception=True)
        sale_return = create_sale_return(request.user, sale, lines.lines(), lines.validated_data['comment'])
        sale_return = Document.objects.prefetch_related(_items).get(pk=sale_return.pk)
        return Response(ReturnSerializer(sale_return).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    list=extend_schema(summary='Есептен шығарулар тізімі'),
    retrieve=extend_schema(summary='Есептен шығару', parameters=[ID]),
)
@extend_schema(tags=['Қойма: есептен шығару'])
class WriteOffViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                      viewsets.GenericViewSet):
    """Есептен шығару: Қоймашы, Басқарушы. Қоймашы өз қоймасының құжаттарын көреді, Басқарушы — барлығын."""

    serializer_class = WriteOffSerializer
    permission_classes = (IsWarehouseStaff,)

    def get_queryset(self):
        if _schema_generation(self):
            return Document.objects.none()
        queryset = (
            Document.objects.filter(doc_type=Document.Type.WRITE_OFF)
            .select_related('warehouse', 'responsible', 'canceled_by')
            .prefetch_related(_items)
            .order_by('-date')
        )
        user = self.request.user
        if not (user.is_superuser or user.role == User.Role.MANAGER):
            queryset = queryset.filter(warehouse_id=user.warehouse_id)
        return queryset

    @extend_schema(
        summary='Есептен шығару',
        description=(
            'Есептен шығару құжатын қызметкердің қоймасында жасап, бірден өткізеді — бір транзакцияда. '
            'Себеп (`comment`) міндетті. Баға — тауардың сату бағасы. Қалдық жетпесе немесе тауар '
            'түгендеуде бұғатталса — 400, ештеңе сақталмайды.'
        ),
        request=WriteOffLinesSerializer,
        responses={201: WriteOffSerializer, **ERROR_RESPONSES},
    )
    def create(self, request, *args, **kwargs):
        lines = WriteOffLinesSerializer(data=request.data)
        lines.is_valid(raise_exception=True)
        document = create_write_off(request.user, lines.lines(), lines.validated_data['comment'])
        return Response(self.get_serializer(self.get_queryset().get(pk=document.pk)).data,
                        status=status.HTTP_201_CREATED)


@extend_schema_view(
    list=extend_schema(
        summary='Құжаттар журналы',
        parameters=[
            OpenApiParameter('doc_type', OpenApiTypes.STR, enum=Document.Type.values),
            OpenApiParameter('status', OpenApiTypes.STR, enum=Document.Status.values),
            OpenApiParameter('warehouse', OpenApiTypes.INT, description='Қойма id'),
            OpenApiParameter('date_from', OpenApiTypes.DATE, description='Құжат күні, бастап (қоса)'),
            OpenApiParameter('date_to', OpenApiTypes.DATE, description='Құжат күні, дейін (қоса)'),
        ],
    ),
    retrieve=extend_schema(summary='Құжат', parameters=[ID]),
)
@extend_schema(tags=['Құжаттар'])
class DocumentViewSet(viewsets.ReadOnlyModelViewSet):
    """Барлық түрдегі құжаттар журналы және күшін жою. Тек Басқарушы. `search` — нөмірі бойынша."""

    serializer_class = DocumentSerializer
    permission_classes = (IsManagerDocuments,)
    filter_backends = (filters.SearchFilter,)
    search_fields = ('number',)

    def get_queryset(self):
        if _schema_generation(self):
            return Document.objects.none()
        queryset = (
            Document.objects.select_related('warehouse', 'supplier', 'responsible', 'canceled_by', 'base_document')
            .prefetch_related(_items, 'returns')
            .order_by('-date', '-id')
        )
        params = DocumentFilterSerializer(data=self.request.query_params)
        params.is_valid(raise_exception=True)
        query = params.validated_data
        for field, lookup in (('doc_type', 'doc_type'), ('status', 'status'), ('warehouse', 'warehouse_id')):
            if query.get(field):
                queryset = queryset.filter(**{lookup: query[field]})
        # Күн шекаралары есептердегідей — жергілікті уақыт белдеуі бойынша, екі шеті де қоса
        if query.get('date_from'):
            queryset = queryset.filter(date__gte=period_bounds(query['date_from'], query['date_from'])[0])
        if query.get('date_to'):
            queryset = queryset.filter(date__lt=period_bounds(query['date_to'], query['date_to'])[1])
        return queryset

    @extend_schema(
        summary='Құжаттың күшін жою',
        description=(
            'DRAFT — жай ғана күші жойылады; POSTED — кері қозғалыстар жазылады, қалдық қалпына келеді, '
            '`reason` міндетті. Өткізілген қайтаруы бар сатудың/қабылдаудың, түгендеуде бұғатталған '
            'тауарлардың немесе кері қозғалыстан кейін теріс қалдық болатын құжаттың күшін жоюға болмайды (400). '
            'Кім және неге жойғаны сақталады.'
        ),
        parameters=[ID],
        request=CancelSerializer,
        responses={200: DocumentSerializer, **ERROR_RESPONSES},
    )
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        data = CancelSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        document = cancel_document(self.get_object(), request.user, data.validated_data['reason'])
        return Response(self.get_serializer(self.get_queryset().get(pk=document.pk)).data)


@extend_schema_view(
    list=extend_schema(summary='Жеткізушілер тізімі'),
    retrieve=extend_schema(summary='Жеткізуші', parameters=[ID]),
)
@extend_schema(tags=['Қойма: қабылдау'])
class SupplierViewSet(viewsets.ReadOnlyModelViewSet):
    """Жеткізушілер (қабылдау формасы үшін). `search` — атауы немесе БСН. Қоймашы, Басқарушы."""

    serializer_class = SupplierSerializer
    permission_classes = (IsWarehouseStaff,)
    filter_backends = (filters.SearchFilter,)
    search_fields = ('name', 'bin')
    queryset = Supplier.objects.order_by('name')


@extend_schema_view(
    list=extend_schema(summary='Қабылдаулар тізімі'),
    retrieve=extend_schema(summary='Қабылдау', parameters=[ID]),
)
@extend_schema(tags=['Қойма: қабылдау'])
class ReceiptViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                     viewsets.GenericViewSet):
    """Жеткізушіден қабылдау және жеткізушіге қайтару: Қоймашы, Басқарушы.

    Қоймашы өз қоймасының қабылдауларын көреді, Басқарушы — барлығын. `search` — нөмірі бойынша дәл іздеу.
    """

    serializer_class = ReceiptSerializer
    permission_classes = (IsWarehouseStaff,)
    filter_backends = (filters.SearchFilter,)
    search_fields = ('=number',)

    def get_queryset(self):
        if _schema_generation(self):
            return Document.objects.none()
        queryset = (
            Document.objects.filter(doc_type=Document.Type.RECEIPT)
            .select_related('warehouse', 'supplier', 'responsible', 'canceled_by')
            .prefetch_related(_items, _returns)
            .order_by('-date')
        )
        user = self.request.user
        if not (user.is_superuser or user.role == User.Role.MANAGER):
            queryset = queryset.filter(warehouse_id=user.warehouse_id)
        return queryset

    @extend_schema(
        summary='Жеткізушіден қабылдау',
        description=(
            'Қабылдау құжатын қызметкердің қоймасында жасап, бірден өткізеді — бір транзакцияда. '
            'Әр жолда сатып алу бағасы көрсетіледі. Тауар түгендеуде бұғатталса — 400, ештеңе сақталмайды.'
        ),
        request=ReceiptRequestSerializer,
        responses={201: ReceiptSerializer, **ERROR_RESPONSES},
    )
    def create(self, request, *args, **kwargs):
        data = ReceiptRequestSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        receipt = create_receipt(request.user, data.validated_data['supplier'], data.lines(),
                                 data.validated_data['comment'])
        return Response(self.get_serializer(self.get_queryset().get(pk=receipt.pk)).data,
                        status=status.HTTP_201_CREATED)

    @extend_schema(
        summary='Жеткізушіге қайтару',
        description=(
            'Жеткізушіге қайтару құжатын жасап, бірден өткізеді. Жеткізуші мен баға қабылдаудан алынады. '
            'Қабылданған саннан (бұрынғы қайтаруларды шегергенде) және қоймадағы қалдықтан артық қайтаруға болмайды.'
        ),
        parameters=[ID],
        request=ReturnLinesSerializer,
        responses={201: ReturnSerializer, **ERROR_RESPONSES},
    )
    @action(detail=True, methods=['post'])
    def returns(self, request, pk=None):
        receipt = self.get_object()
        lines = ReturnLinesSerializer(data=request.data)
        lines.is_valid(raise_exception=True)
        supplier_return = create_supplier_return(request.user, receipt, lines.lines(),
                                                 lines.validated_data['comment'])
        supplier_return = Document.objects.prefetch_related(_items).get(pk=supplier_return.pk)
        return Response(ReturnSerializer(supplier_return).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    list=extend_schema(
        summary='Түгендеулер тізімі',
        parameters=[OpenApiParameter('status', OpenApiTypes.STR, enum=Document.Status.values)],
    ),
    retrieve=extend_schema(summary='Түгендеу', parameters=[ID]),
)
@extend_schema(tags=['Түгендеу'])
class InventoryViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                       viewsets.GenericViewSet):
    """Түгендеу: бастау → нақты санды енгізу (Қоймашы) → бекіту немесе күшін жою (Басқарушы).

    Қоймашы өз қоймасының түгендеулерін көреді, Басқарушы — барлығын.
    """

    serializer_class = InventorySerializer
    permission_classes = (IsInventoryStaff,)

    def get_queryset(self):
        if _schema_generation(self):
            return Document.objects.none()
        items = Prefetch(
            'items', queryset=DocumentItem.objects.select_related('product').order_by('product__name')
        )
        queryset = (
            Document.objects.filter(doc_type=Document.Type.INVENTORY)
            .select_related('warehouse', 'responsible', 'canceled_by')
            .prefetch_related(items)
            .order_by('-date')
        )
        user = self.request.user
        if not (user.is_superuser or user.role == User.Role.MANAGER):
            queryset = queryset.filter(warehouse_id=user.warehouse_id)
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        return queryset

    def _respond(self, document, code=status.HTTP_200_OK):
        return Response(self.get_serializer(self.get_queryset().get(pk=document.pk)).data, status=code)

    @extend_schema(
        summary='Түгендеуді бастау',
        description=(
            'DRAFT түгендеу құжатын жасайды, әр тауарға сол сәттегі есептік қалдық жазылады. '
            'Сол сәттен бастап бұл тауарлар бойынша кез келген қозғалыс (сату, қайтару, күшін жою, …) '
            'бекітілгенше немесе күші жойылғанша бұғатталады. Қоймашы, Басқарушы.'
        ),
        request=StartInventorySerializer,
        responses={201: InventorySerializer, **ERROR_RESPONSES},
    )
    def create(self, request, *args, **kwargs):
        data = StartInventorySerializer(data=request.data)
        data.is_valid(raise_exception=True)
        document = start_inventory(request.user, data.validated_data['products'], data.validated_data['comment'])
        return self._respond(document, status.HTTP_201_CREATED)

    @extend_schema(
        summary='Нақты санды енгізу',
        description='Саналған санды енгізеді (0 болуы мүмкін). Қайта енгізілсе — соңғысы сақталады. Тек DRAFT.',
        parameters=[ID],
        request=CountsSerializer,
        responses={200: InventorySerializer, **ERROR_RESPONSES},
    )
    @action(detail=True, methods=['post'])
    def counts(self, request, pk=None):
        document = self.get_object()
        lines = CountsSerializer(data=request.data)
        lines.is_valid(raise_exception=True)
        record_counts(request.user, document, lines.lines())
        return self._respond(document)

    @extend_schema(
        summary='Түгендеуді бекіту',
        description=(
            'Қалдықты нақты санға теңестіреді, айырма StockMovement журналына жазылады. '
            'Барлық тауар саналған болуы тиіс. Тек Басқарушы. `comment` — айырманың түсіндірмесі (міндетті емес).'
        ),
        parameters=[ID],
        request=ApproveSerializer,
        responses={200: InventorySerializer, **ERROR_RESPONSES},
    )
    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        data = ApproveSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        document = post_document(self.get_object(), request.user, comment=data.validated_data.get('comment'))
        return self._respond(document)

    @extend_schema(
        summary='Түгендеудің күшін жою',
        description=(
            'DRAFT — бұғат алынады; POSTED — кері қозғалыстар жазылады, `reason` міндетті. Тек Басқарушы. '
            'Кім және неге жойғаны сақталады (`canceled_by`, `cancel_reason`).'
        ),
        parameters=[ID],
        request=CancelSerializer,
        responses={200: InventorySerializer, **ERROR_RESPONSES},
    )
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        data = CancelSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        document = cancel_document(self.get_object(), request.user, data.validated_data['reason'])
        return self._respond(document)
