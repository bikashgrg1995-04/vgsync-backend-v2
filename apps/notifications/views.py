from decimal import Decimal

from django.db.models import DecimalField, Exists, F, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts import rbac
from apps.common.permissions import HasActiveBusinessContext
from apps.sales.models import Followup

from .models import Notification, NotificationRead
from .serializers import NotificationSerializer
from .services import refresh


def owed_on_purchase():
    """What is still unpaid on the notification's purchase (0 if none)."""
    from apps.purchases.models import CreditSettlement, PurchasePayment

    def total(model, **where):
        return Coalesce(
            Subquery(
                model.objects.filter(purchase=OuterRef("purchase"), **where)
                .values("purchase")
                .annotate(t=Sum("amount"))
                .values("t")[:1],
                output_field=DecimalField(max_digits=14, decimal_places=2),
            ),
            Decimal("0"),
            output_field=DecimalField(max_digits=14, decimal_places=2),
        )

    return F("purchase__net_total") - total(
        PurchasePayment, status=PurchasePayment.Status.ACTIVE
    ) - total(
        CreditSettlement,
        kind=CreditSettlement.Kind.ADJUSTMENT,
        status=CreditSettlement.Status.ACTIVE,
    )


def sees_due_alerts(user):
    """The owner, and staff who manage sales or inventory."""
    return any(
        rbac.has_permission(user, code) for code in ("sales.update", "inventory.update")
    )


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Your notifications in the active business, newest first: tasks handed to
    you, task updates, and (if you may see reminders) follow-ups that are due
    due soon, today or overdue and still pending. They are made on the fly (at most
    every few minutes) and by the ``generate_notifications`` command.

    Filters: ``unread=true`` or ``false`` (for you), ``kind``.
    """

    permission_classes = [IsAuthenticated, HasActiveBusinessContext]
    serializer_class = NotificationSerializer

    filterset_fields = ("kind",)
    search_fields = ("title", "body")
    ordering_fields = ("id", "due_date")
    ordering = ("-id",)

    def get_queryset(self):
        refresh(self.request.active_business)

        user = self.request.user
        business = self.request.active_business
        scope = Q(recipient=user)

        # Follow-up and credit due dates are for the owner and for those who
        # manage sales or inventory. Once the money is in or the call is
        # made, a follow-up is no longer news.
        if sees_due_alerts(user):
            scope |= Q(recipient__isnull=True, followup__status=Followup.Status.PENDING)
            scope |= Q(
                recipient__isnull=True,
                purchase__isnull=False,
                purchase__status__in=["partial", "received"],
                still_owed__gt=0,
            )

        queryset = (
            Notification.objects.annotate(still_owed=owed_on_purchase())
            .filter(scope, business=business)
            .select_related("followup", "task")
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
