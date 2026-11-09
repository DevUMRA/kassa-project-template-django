from django.core.exceptions import PermissionDenied
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import reports
from ..models import Document, User
from . import report_schemas as schemas
from .exceptions import ERROR_RESPONSES
from .permissions import IsInventoryStaff, IsManager

TAG = ['Есептер']


class PeriodSerializer(serializers.Serializer):
    """Кезең: date_from..date_to (екеуі де қоса). Әдепкі — ағымдағы айдың басынан бүгінге дейін."""

    date_from = serializers.DateField(required=False, help_text='Әдепкі — ағымдағы айдың 1-күні')
    date_to = serializers.DateField(required=False, help_text='Әдепкі — бүгін')
    warehouse = serializers.IntegerField(
        required=False, help_text='Қойма id. Басқарушы үшін — көрсетілмесе барлық қойма; Қоймашы — тек өз қоймасы'
    )
    product = serializers.IntegerField(required=False, help_text='Тауар id')

    def validate(self, attrs):
        today = timezone.localdate()
        attrs.setdefault('date_to', today)
        attrs.setdefault('date_from', attrs['date_to'].replace(day=1))
        if attrs['date_from'] > attrs['date_to']:
            raise serializers.ValidationError('date_from date_to-дан кейін болмауы тиіс.')
        attrs['start'], attrs['end'] = reports.period_bounds(attrs['date_from'], attrs['date_to'])
        return attrs


class ReportMixin:
    """Басқарушы — кез келген қойма (?warehouse=, көрсетілмесе — барлығы); Қоймашы — тек өз қоймасы."""

    permission_classes = (IsInventoryStaff,)
    params_class = PeriodSerializer

    def params(self):
        serializer = self.params_class(data=self.request.query_params)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    def warehouse_ids(self, params):
        user = self.request.user
        requested = params.get('warehouse')
        if user.is_superuser or user.role == User.Role.MANAGER:
            return None if requested is None else [requested]
        if user.warehouse_id is None:
            raise PermissionDenied('Қызметкерге қойма бекітілмеген.')
        if requested is not None and requested != user.warehouse_id:
            raise PermissionDenied('Тек өз қоймаңыздың есебін көре аласыз.')
        return [user.warehouse_id]

    @staticmethod
    def period(params):
        return {'date_from': params['date_from'], 'date_to': params['date_to']}


class StockParamsSerializer(serializers.Serializer):
    warehouse = serializers.IntegerField(required=False, help_text='Қойма id (Қоймашы — тек өз қоймасы)')
    search = serializers.CharField(required=False, help_text='Атауы, артикулы немесе штрихкоды')
    low = serializers.DecimalField(
        max_digits=12, decimal_places=3, required=False, help_text='Осы саннан аз немесе тең қалдықтар ғана'
    )


class StockReportView(ReportMixin, APIView):
    params_class = StockParamsSerializer

    @extend_schema(
        tags=TAG, summary='Қалдықтар', parameters=[StockParamsSerializer],
        description='Ағымдағы қалдық және оның сату бағасымен құны. Басқарушы, Қоймашы.',
        responses={200: schemas.StockReportSerializer, **ERROR_RESPONSES},
    )
    def get(self, request):
        params = self.params()
        return Response(reports.stock_report(self.warehouse_ids(params), params.get('search'), params.get('low')))


class MovementParamsSerializer(PeriodSerializer):
    doc_type = serializers.ChoiceField(choices=Document.Type.choices, required=False, help_text='Құжат түрі')
    page = serializers.IntegerField(required=False, help_text='Бет нөмірі (бетте 50 жазба)')


class MovementReportView(ReportMixin, GenericAPIView):
    params_class = MovementParamsSerializer
    serializer_class = schemas.MovementRowSerializer

    @extend_schema(
        tags=TAG, summary='Қозғалыс журналы', parameters=[MovementParamsSerializer],
        description='Кезең ішіндегі StockMovement жазбалары, жаңалары бірінші, беттеп. Басқарушы, Қоймашы.',
        responses={200: schemas.MovementPageSerializer, **ERROR_RESPONSES},
    )
    def get(self, request):
        params = self.params()
        movements = reports.movements_queryset(
            params['start'], params['end'], self.warehouse_ids(params), params.get('product'), params.get('doc_type'),
        )
        page = self.paginate_queryset(movements)
        response = self.get_paginated_response([reports.movement_row(movement) for movement in page])
        response.data = {**self.period(params), **response.data}
        return response


class TurnoverReportView(ReportMixin, APIView):
    @extend_schema(
        tags=TAG, summary='Айналым ведомосі', parameters=[PeriodSerializer],
        description=(
            'Тауар × қойма бойынша: бастапқы қалдық + кіріс − шығыс = соңғы қалдық. '
            'Күшін жоюдан туған кері жазбалар бағыты бойынша кіріс/шығысқа түседі. Басқарушы, Қоймашы.'
        ),
        responses={200: schemas.TurnoverReportSerializer, **ERROR_RESPONSES},
    )
    def get(self, request):
        params = self.params()
        report = reports.turnover_report(
            params['start'], params['end'], self.warehouse_ids(params), params.get('product'),
        )
        return Response({**self.period(params), **report})


class SalesParamsSerializer(PeriodSerializer):
    group_by = serializers.ChoiceField(
        choices=list(reports.SALES_GROUPS), required=False, default='day', help_text='Топтау'
    )


class SalesReportView(ReportMixin, APIView):
    permission_classes = (IsManager,)
    params_class = SalesParamsSerializer

    @extend_schema(
        tags=TAG, summary='Сату есебі', parameters=[SalesParamsSerializer],
        description=(
            'Өткізілген сатулар мен сатушылық қайтарулар (posted_at бойынша), түсім = сату − қайтару. '
            'Күші жойылған құжаттар кірмейді. Тек Басқарушы.'
        ),
        responses={200: schemas.SalesReportSerializer, **ERROR_RESPONSES},
    )
    def get(self, request):
        params = self.params()
        report = reports.sales_report(params['start'], params['end'], self.warehouse_ids(params), params['group_by'])
        return Response({**self.period(params), **report})


class InventoryReportView(ReportMixin, APIView):
    @extend_schema(
        tags=TAG, summary='Түгендеу нәтижелері', parameters=[PeriodSerializer],
        description=(
            'Бекітілген түгендеулердің кемшілігі мен артығы (журналдағы нақты айырма) '
            'және кезеңдегі есептен шығарулар. Басқарушы, Қоймашы.'
        ),
        responses={200: schemas.InventoryReportSerializer, **ERROR_RESPONSES},
    )
    def get(self, request):
        params = self.params()
        report = reports.inventory_report(params['start'], params['end'], self.warehouse_ids(params))
        return Response({**self.period(params), **report})
