from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.common.dates import parse_query_date
from apps.common.exceptions import Conflict
from apps.common.permissions import IsOwner

from .models import Expense, Income
from .serializers import ExpenseSerializer, IncomeSerializer, VoidSerializer


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

        if day := parse_query_date(query.get("expense_date__gte")):
            refunds = refunds.filter(settled_on__gte=day)

        if day := parse_query_date(query.get("expense_date__lte")):
            refunds = refunds.filter(settled_on__lte=day)

        return refunds.aggregate(total=Sum("amount"))["total"] or Decimal("0")


class IncomeViewSet(viewsets.ModelViewSet):
    """
    Income of the active business. Every customer payment on a sale is added
    here automatically (read-only); other income is entered by the owner.
    Income is voided, not deleted.
    """

    permission_classes = [IsOwner]
    serializer_class = IncomeSerializer
    # Manual income is entered once and voided if wrong, never edited.
    http_method_names = ["get", "post", "head", "options"]

    filterset_fields = {
        "category": ["exact"],
        "status": ["exact"],
        "payment_method": ["exact"],
        "income_date": ["gte", "lte"],
    }
    search_fields = ("description", "reference")
    ordering_fields = ("income_date", "amount", "created_at")

    def get_queryset(self):
        return Income.objects.filter(business=self.request.active_business)

    def perform_create(self, serializer):
        serializer.save(
            business=self.request.active_business,
            created_by=self.request.user,
            income_date=serializer.validated_data.get(
                "income_date", timezone.localdate()
            ),
        )

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        income = self.get_object()

        if income.sale_payment_id:
            raise Conflict(
                "This income comes from a customer payment. Void the "
                "payment to remove it.",
                code="payment_income_locked",
            )

        if income.status != Income.Status.ACTIVE:
            raise Conflict("This income is already void.", code="invalid_status")

        serializer = VoidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        income.status = Income.Status.VOID
        income.voided_by = request.user
        income.voided_at = timezone.now()
        income.void_reason = serializer.validated_data["reason"]
        income.save(update_fields=["status", "voided_by", "voided_at", "void_reason"])

        return Response(self.get_serializer(income).data)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """
        Totals by category for the filtered, active income, and the same
        total net of refunds paid back to customers (``net_total``).
        """
        queryset = self.filter_queryset(self.get_queryset()).filter(
            status=Income.Status.ACTIVE
        )

        rows = (
            queryset.values("category")
            .annotate(total=Sum("amount"), count=Count("id"))
            .order_by("category")
        )

        total = sum((row["total"] for row in rows), Decimal("0"))
        refunds = self._sale_refunds(request)

        return Response({
            "total": f"{total:.2f}",
            "sale_refunds": f"{refunds:.2f}",
            "net_total": f"{total - refunds:.2f}",
            "by_category": [
                {
                    "category": row["category"],
                    "total": f"{row['total']:.2f}",
                    "count": row["count"],
                }
                for row in rows
            ],
        })

    @staticmethod
    def _sale_refunds(request):
        """
        Cash paid back to customers for returned goods in the same period.
        It reduces what was really earned. A return, credit set against a
        sale, or replacement goods move no cash and change nothing here.
        A refund belongs to a customer, not to one kind of sale, so a
        ``category`` filter leaves refunds out.
        """
        from apps.sales.models import SaleCreditSettlement

        query = request.query_params

        if query.get("category") not in (None, ""):
            return Decimal("0")

        refunds = SaleCreditSettlement.objects.filter(
            business=request.active_business,
            kind=SaleCreditSettlement.Kind.REFUND,
            status=SaleCreditSettlement.Status.ACTIVE,
        )

        if day := parse_query_date(query.get("income_date__gte")):
            refunds = refunds.filter(settled_on__gte=day)

        if day := parse_query_date(query.get("income_date__lte")):
            refunds = refunds.filter(settled_on__lte=day)

        return refunds.aggregate(total=Sum("amount"))["total"] or Decimal("0")
