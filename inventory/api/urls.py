from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from rest_framework.authtoken.views import obtain_auth_token
from rest_framework.routers import DefaultRouter, SimpleRouter

from .reports import (
    InventoryReportView,
    MovementReportView,
    SalesReportView,
    StockReportView,
    TurnoverReportView,
)
from .views import (
    DocumentViewSet,
    InventoryViewSet,
    ProductViewSet,
    ReceiptViewSet,
    SaleViewSet,
    SupplierViewSet,
    WriteOffViewSet,
    health,
    me,
    warehouses,
)

router = DefaultRouter()
router.register('products', ProductViewSet, basename='pos-product')
router.register('sales', SaleViewSet, basename='pos-sale')

inventory_router = DefaultRouter()
inventory_router.register('audits', InventoryViewSet, basename='inventory-audit')

# SimpleRouter: '' префиксінде DefaultRouter-дің түбір (api-root) беті /api/-ді алып қоймасын
documents_router = SimpleRouter()
documents_router.register('documents', DocumentViewSet, basename='document')

warehouse_router = DefaultRouter()
warehouse_router.register('write-offs', WriteOffViewSet, basename='warehouse-write-off')
warehouse_router.register('receipts', ReceiptViewSet, basename='warehouse-receipt')
warehouse_router.register('suppliers', SupplierViewSet, basename='warehouse-supplier')

urlpatterns = [
    path('health/', health, name='api-health'),
    path('me/', me, name='api-me'),
    path('warehouses/', warehouses, name='api-warehouses'),
    path('schema/',SpectacularAPIView.as_view(), name='api-schema'),
    path('docs/', SpectacularSwaggerView.as_view(url_name='api-schema'), name='api-docs'),
    path('redoc/', SpectacularRedocView.as_view(url_name='api-schema'), name='api-redoc'),
    path('auth/token/', obtain_auth_token, name='api-token'),
    path('pos/', include(router.urls)),
    path('inventory/', include(inventory_router.urls)),
    path('warehouse/', include(warehouse_router.urls)),
    path('', include(documents_router.urls)),
    path('reports/stock/', StockReportView.as_view(), name='report-stock'),
    path('reports/movements/', MovementReportView.as_view(), name='report-movements'),
    path('reports/turnover/', TurnoverReportView.as_view(), name='report-turnover'),
    path('reports/sales/', SalesReportView.as_view(), name='report-sales'),
    path('reports/inventory/', InventoryReportView.as_view(), name='report-inventory'),
]
