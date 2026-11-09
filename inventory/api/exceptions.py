from django.core.exceptions import PermissionDenied, ValidationError
from drf_spectacular.utils import OpenApiResponse, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import ParseError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

# OpenAPI: сервис қателерінің пішіні
ERROR_RESPONSES = {
    400: OpenApiResponse(
        inline_serializer('BusinessError', {'detail': serializers.ListField(child=serializers.CharField())}),
        description='Бизнес-ереже бұзылды (қалдық жетпейді, түгендеу бұғаты, …) немесе параметр қате',
    ),
    403: OpenApiResponse(
        inline_serializer('Forbidden', {'detail': serializers.CharField()}),
        description='Рөлге рұқсат жоқ',
    ),
}


def exception_handler(exc, context):
    """Сервис қабатының Django қателерін API жауабына айналдырады (DRF оларды хабарламасыз береді)."""
    if isinstance(exc, ValidationError):
        return Response({'detail': exc.messages}, status=status.HTTP_400_BAD_REQUEST)
    if isinstance(exc, PermissionDenied) and exc.args:
        return Response({'detail': str(exc)}, status=status.HTTP_403_FORBIDDEN)
    if isinstance(exc, ParseError):
        # DRF-тің «JSON parse error» хабарының аудармасы жоқ, ал ішкі мәтіні (кодек қатесі) пайдаланушыға түсініксіз
        return Response(
            {'detail': 'Сұраныс денесі оқылмады: JSON пішімі мен UTF-8 кодтауын тексеріңіз.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return drf_exception_handler(exc, context)
