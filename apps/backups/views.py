from django.http import HttpResponse
from django.utils import timezone
from rest_framework import mixins, viewsets
from rest_framework.response import Response

from apps.common.permissions import IsOwner

from .models import BackupLog
from .serializers import BackupLogSerializer
from .services import build_backup


class BackupViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    ``GET`` lists the backups taken for this business. ``POST`` builds a
    backup and returns it as a ZIP download. Owner only.

    The company owner's backup, taken in the Head Office, covers every
    branch too. Restoring is not offered.
    """

    permission_classes = [IsOwner]
    serializer_class = BackupLogSerializer

    def get_queryset(self):
        return BackupLog.objects.filter(business=self.request.active_business)

    def create(self, request, *args, **kwargs):
        business = request.active_business
        data, summary = build_backup(request.user, business)

        BackupLog.objects.create(
            business=business,
            created_by=request.user,
            created_by_name=request.user.get_full_name() or request.user.username,
            **summary,
        )

        name = f"vgsync-backup-{business.business_code}-{timezone.localtime():%Y%m%d-%H%M}.zip"
        response = HttpResponse(data, content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="{name}"'
        response["Access-Control-Expose-Headers"] = "Content-Disposition"

        return response
