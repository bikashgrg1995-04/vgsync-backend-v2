from django.http import HttpResponse
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from apps.common.permissions import HasModulePermission

from .models import BackupLog
from .serializers import BackupLogSerializer
from .services import (
    RestoreError,
    apply_restore,
    build_backup,
    businesses_in_scope,
    preview_restore,
)

MAX_UPLOAD = 50 * 1024 * 1024
KEEP_SAFETY_COPIES = 3


def zip_response(data, name):
    response = HttpResponse(data, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    response["Access-Control-Expose-Headers"] = "Content-Disposition"

    return response


class BackupViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    The owner, and staff whose position allows it (the Administrator
    position does), can use this.

    * ``GET``: the backups taken and restores done for this business.
    * ``POST``: build a backup and download it as a ZIP.
    * ``POST restore-preview/``: read an uploaded backup and say what a
      restore would replace. Changes nothing.
    * ``POST restore/``: replace this business's records with the backup.
      A safety copy of the current data is kept first, so it can be undone.

    The company owner's backup, taken in the Head Office, covers every
    branch too. Sign-in accounts and permissions are never restored.
    """

    permission_classes = [HasModulePermission]
    permission_module = "backup"
    permission_actions = {
        "create": "create",
        "preview": "restore",
        "restore": "restore",
        "download": "restore",
    }
    serializer_class = BackupLogSerializer

    def get_queryset(self):
        queryset = BackupLog.objects.filter(business=self.request.active_business)

        return queryset.defer("data") if self.action == "list" else queryset

    def _who(self, request):
        return request.user.get_full_name() or request.user.username

    def create(self, request, *args, **kwargs):
        business = request.active_business
        data, summary = build_backup(request.user, business)

        BackupLog.objects.create(
            business=business,
            created_by=request.user,
            created_by_name=self._who(request),
            **summary,
        )

        name = f"vgsync-backup-{business.business_code}-{timezone.localtime():%Y%m%d-%H%M}.zip"

        return zip_response(data, name)

    def _upload(self, request):
        file = request.FILES.get("file")

        if file is None:
            raise RestoreError("Choose the backup file to restore from.")

        if file.size > MAX_UPLOAD:
            raise RestoreError("This file is too large to be a backup.")

        return file.read()

    @action(
        detail=False,
        methods=["post"],
        url_path="restore-preview",
        parser_classes=[MultiPartParser, FormParser],
    )
    def preview(self, request):
        try:
            result = preview_restore(
                request.user, request.active_business, self._upload(request)
            )
        except RestoreError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(result)

    @action(
        detail=False,
        methods=["post"],
        parser_classes=[MultiPartParser, FormParser],
    )
    def restore(self, request):
        business = request.active_business

        # Typing the business code makes this a deliberate act.
        if request.data.get("confirm") != business.business_code:
            return Response(
                {"detail": f"Type {business.business_code} to confirm the restore."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            data = self._upload(request)
            preview_restore(request.user, business, data)  # validates the file

            safety, summary = build_backup(request.user, business)
            apply_restore_result = apply_restore(request.user, business, data)
        except RestoreError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)

        # Notifications were cleared with the restore: make them again from
        # the restored follow-ups, payments and tasks.
        from apps.notifications.services import generate_for_business, generate_payables

        for scoped in businesses_in_scope(request.user, business):
            generate_for_business(scoped)
            generate_payables(scoped)

        # Only after the restore went through: keep the copy that undoes it.
        BackupLog.objects.create(
            business=business,
            created_by=request.user,
            created_by_name=self._who(request),
            kind=BackupLog.Kind.SAFETY,
            data=safety,
            **summary,
        )
        BackupLog.objects.create(
            business=business,
            created_by=request.user,
            created_by_name=self._who(request),
            kind=BackupLog.Kind.RESTORE,
            row_count=apply_restore_result,
            scope=summary["scope"],
            business_count=summary["business_count"],
        )

        old = BackupLog.objects.filter(
            business=business, kind=BackupLog.Kind.SAFETY
        ).order_by("-id")[KEEP_SAFETY_COPIES:]
        BackupLog.objects.filter(pk__in=[o.pk for o in old]).delete()

        return Response({"restored": apply_restore_result})

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        log = self.get_object()

        if log.kind != BackupLog.Kind.SAFETY or log.data is None:
            return Response(status=status.HTTP_404_NOT_FOUND)

        return zip_response(
            bytes(log.data),
            f"vgsync-safety-copy-{timezone.localtime(log.created_at):%Y%m%d-%H%M}.zip",
        )
