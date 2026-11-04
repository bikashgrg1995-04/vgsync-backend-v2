from rest_framework import viewsets

from apps.common.permissions import IsOwner

from .models import AuditLog
from .serializers import AuditLogSerializer


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    The change history of the active business, newest first. Owner only.

    Filters: ``model`` (for example ``sales.sale``), ``object_id`` (with
    ``model``, the whole history of one record), ``action``, ``actor`` and
    ``created_at__gte`` / ``created_at__lte`` (a date or date and time).
    ``search`` looks at the record's name and the user's name.
    """

    permission_classes = [IsOwner]
    serializer_class = AuditLogSerializer

    filterset_fields = {
        "model": ["exact"],
        "object_id": ["exact"],
        "action": ["exact"],
        "actor": ["exact"],
        "created_at": ["gte", "lte"],
    }
    search_fields = ("object_repr", "actor_name")
    ordering_fields = ("id", "created_at")
    ordering = ("-id",)

    def get_queryset(self):
        return AuditLog.objects.filter(business=self.request.active_business)
