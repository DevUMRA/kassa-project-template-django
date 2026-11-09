from rest_framework.permissions import BasePermission

from ..models import User


class IsCashier(BasePermission):
    """Кассалық операциялар: Сатушы, Басқарушы немесе суперпайдаланушы."""

    message = 'Кассалық операциялар тек Сатушы мен Басқарушыға рұқсат.'

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user and user.is_authenticated
            and (user.is_superuser or user.role in (User.Role.SELLER, User.Role.MANAGER))
        )


class IsManager(BasePermission):
    message = 'Бұл есеп тек Басқарушыға рұқсат.'

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and (user.is_superuser or user.role == User.Role.MANAGER))


class IsManagerDocuments(IsManager):
    message = 'Құжаттар журналы мен күшін жою тек Басқарушыға рұқсат.'


class IsInventoryStaff(BasePermission):
    """Түгендеу: Қоймашы (санайды), Басқарушы (бекітеді) немесе суперпайдаланушы."""

    message = 'Түгендеу тек Қоймашы мен Басқарушыға рұқсат.'

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user and user.is_authenticated
            and (user.is_superuser or user.role in (User.Role.WAREHOUSEMAN, User.Role.MANAGER))
        )


class IsWarehouseStaff(IsInventoryStaff):
    """Қоймалық операциялар (есептен шығару): Қоймашы, Басқарушы немесе суперпайдаланушы."""

    message = 'Қоймалық операциялар тек Қоймашы мен Басқарушыға рұқсат.'
