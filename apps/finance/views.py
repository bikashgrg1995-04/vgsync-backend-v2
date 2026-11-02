from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.common.exceptions import Conflict
from apps.common.permissions import IsOwner

from .models import Expense
from .serializers import ExpenseSerializer, VoidSerializer


class ExpenseViewSet(viewsets.ModelViewSet):
    """
    Expenses of the active business. Salary payments are added here
    automatically; other expenses are entered by the owner. Expenses are
    voided, not deleted.
    """

    permission_classes = [IsOwner]
    serializer_class = ExpenseSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    filterset_fields = {
        "category": ["exact"],
        "status": ["exact"],
        "payment_method": ["exact"],
        "expense_date": ["gte", "lte"],
    }
    search_fields = ("description", "reference")
    ordering_fields = ("expense_date", "amount", "created_at")

    def get_queryset(self):
        return Expense.objects.filter(business=self.request.active_business)

    def perform_create(self, serializer):
        serializer.save(
            business=self.request.active_business,
            created_by=self.request.user,
            expense_date=serializer.validated_data.get(
                "expense_date", timezone.localdate()
            ),
        )

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        expense = self.get_object()

        if expense.payroll_payment_id or expense.purchase_payment_id:
            raise Conflict(
                "This expense comes from a payment. Void the payment to "
                "remove it.",
                code="payment_expense_locked",
            )

        if expense.status != Expense.Status.ACTIVE:
            raise Conflict("This expense is already void.", code="invalid_status")

        serializer = VoidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        expense.status = Expense.Status.VOID
        expense.voided_by = request.user
        expense.voided_at = timezone.now()
        expense.void_reason = serializer.validated_data["reason"]
        expense.save(
            update_fields=["status", "voided_by", "voided_at", "void_reason"]
        )

        return Response(self.get_serializer(expense).data)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """
        Totals by category for the filtered, active expenses, and the same
        total net of supplier refunds (``net_total``).
        """
        queryset = self.filter_queryset(self.get_queryset()).filter(
            status=Expense.Status.ACTIVE
        )

        rows = (
            queryset.values("category")
            .annotate(total=Sum("amount"), count=Count("id"))
            .order_by("category")
        )

        by_category = [
            {
                "category": row["category"],
                "total": f"{row['total']:.2f}",
                "count": row["count"],
            }
            for row in rows
        ]

        total = sum((row["total"] for row in rows), Decimal("0"))

        refunds = self._purchase_refunds(request)

        # Money goes out as strings, like everywhere else in the API.
        return Response({
            "total": f"{total:.2f}",
            "purchase_refunds": f"{refunds:.2f}",
            "net_total": f"{total - refunds:.2f}",
            "by_category": by_category,
        })

    @staticmethod
    def _purchase_refunds(request):
        """
        Cash that suppliers paid back for returned goods in the same period.
        It reduces what was really spent. Returning goods, or applying credit
        to a purchase, moves no cash and changes nothing here.
        """
        from apps.purchases.models import CreditSettlement

        query = request.query_params

        # A category filter other than purchase excludes refunds.
        if query.get("category") not in (None, "", Expense.Category.PURCHASE):
            return Decimal("0")

        refunds = CreditSettlement.objects.filter(
            supplier__business=request.active_business,
            kind=CreditSettlement.Kind.REFUND,
            status=CreditSettlement.Status.ACTIVE,
        )

        if day := parse_date(query.get("expense_date__gte") or ""):
            refunds = refunds.filter(settled_on__gte=day)

        if day := parse_date(query.get("expense_date__lte") or ""):
            refunds = refunds.filter(settled_on__lte=day)

        return refunds.aggregate(total=Sum("amount"))["total"] or Decimal("0")
