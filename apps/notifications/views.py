from django.db.models import Exists, OuterRef
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.permissions import HasActiveBusinessContext, HasModulePermission
from apps.sales.models import Followup

from .models import Notification, NotificationRead
from .serializers import NotificationSerializer
from .services import refresh


class CanViewReminders(HasModulePermission):
    """
    Whoever may view reminders may read and mark notifications, since
    marking one read only changes that person's own view of it.
    """

    ACTION_FIELDS = {
        method: "can_view"
        for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
    }


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Notifications for the active business, newest first: follow-ups that are
    due today or overdue and still pending. They are made on the fly (at most
    every few minutes) and by the ``generate_notifications`` command.

    Filters: ``unread=true`` or ``false`` (for you), ``kind``.
    """

    permission_classes = [IsAuthenticated, HasActiveBusinessContext, CanViewReminders]
    permission_module = "reminders"
    serializer_class = NotificationSerializer

    filterset_fields = ("kind",)
    search_fields = ("title", "body")
    ordering_fields = ("id", "due_date")
    ordering = ("-id",)

    def get_queryset(self):
        refresh(self.request.active_business)

        queryset = (
            Notification.objects.filter(
                business=self.request.active_business,
                # Once the money is in or the call is made, it is no longer news.
                followup__status=Followup.Status.PENDING,
            )
            .select_related("followup")
            .annotate(
                is_read=Exists(
                    NotificationRead.objects.filter(
                        notification=OuterRef("pk"), user=self.request.user
                    )
                )
            )
        )

        unread = self.request.query_params.get("unread")

        if unread == "true":
            queryset = queryset.filter(is_read=False)
        elif unread == "false":
            queryset = queryset.filter(is_read=True)

        return queryset

    @action(detail=False, methods=["get"], url_path="unread-count")
    def unread_count(self, request):
        queryset = self.get_queryset().filter(is_read=False)

        return Response({"unread": queryset.count()})

    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        notification = self.get_object()

        NotificationRead.objects.get_or_create(
            notification=notification, user=request.user
        )

        return Response(self.get_serializer(self.get_queryset().get(pk=pk)).data)

    @action(detail=False, methods=["post"], url_path="read-all")
    def read_all(self, request):
        unread = list(self.get_queryset().filter(is_read=False))

        NotificationRead.objects.bulk_create(
            [NotificationRead(notification=n, user=request.user) for n in unread],
            ignore_conflicts=True,
        )

        return Response({"marked": len(unread)})
