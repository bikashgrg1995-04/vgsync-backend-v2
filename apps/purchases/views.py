"""
Purchase APIs, scoped to the active business and to the ``purchases``
module permission. Writes need full access to the business.
"""

from decimal import Decimal

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import (
    DecimalField,
    Prefetch,
    ExpressionWrapper,
    F,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.permissions import HasActiveBusinessContext, HasModulePermission

from .models import (
    CreditSettlement,
    Purchase,
    PurchaseItem,
    PurchasePayment,
    PurchaseReturn,
    Supplier,
)
from .returns import (
    create_return,
    create_settlement,
    void_return,
    void_settlement,
)
from .serializers import (
    CreditSettlementSerializer,
    PurchaseDetailSerializer,
    PurchasePatchSerializer,
    PurchasePaymentSerializer,
    PurchaseReturnSerializer,
    PurchaseReturnWriteSerializer,
    PurchaseSerializer,
    PurchaseWriteSerializer,
    ReasonSerializer,
    SupplierSerializer,
)
from .services import (
    cancel_purchase,
    create_payment,
    create_purchase,
    receive_purchase,
    update_purchase,
    void_payment,
)

MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)
ZERO = Value(Decimal("0.00"), output_field=MONEY_FIELD)


class PurchaseBaseViewSet(viewsets.ModelViewSet):
    permission_classes = [
        IsAuthenticated,
        HasActiveBusinessContext,
        HasModulePermission,
    ]
    permission_module = "purchases"

    def handle_exception(self, exc):
        # The inventory services raise Django errors (for example "product
        # is inactive" or "read-only access"); show them as proper API errors.
        if isinstance(exc, DjangoValidationError):
            exc = ValidationError(detail=exc.messages)

        elif isinstance(exc, DjangoPermissionDenied):
            exc = PermissionDenied(detail=str(exc))

        return super().handle_exception(exc)


def sum_of(queryset, key, field="net_total"):
    """A subquery: the total of ``field`` per supplier, 0 when none."""
    return Coalesce(
        Subquery(
            queryset.values(key)
            .annotate(total=Sum(field))
            .values("total")[:1],
            output_field=MONEY_FIELD,
        ),
        ZERO,
    )


# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------

class SupplierViewSet(PurchaseBaseViewSet):
    """
    Suppliers of the active business, with what is owed to each one.
    ``DELETE`` deactivates; a supplier with purchases is never removed.

    ``outstanding`` counts received purchases not yet paid for (after
    payments and applied credit); ``advance_paid`` is money already paid on
    purchases not yet received; ``credit_balance`` is the credit from
    returned goods that has not been refunded or applied yet. It is kept
    apart from ``outstanding``.
    """

    serializer_class = SupplierSerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    filterset_fields = ("is_active",)
    search_fields = ("name", "contact_person", "phone", "pan_number")
    ordering_fields = ("name", "created_at", "outstanding")
    ordering = ("name",)

    def get_queryset(self):
        active_payment = PurchasePayment.Status.ACTIVE
        active_credit = CreditSettlement.Status.ACTIVE
        received = Purchase.Status.RECEIVED

        return Supplier.objects.filter(
            business=self.request.active_business
        ).annotate(
            total_purchased=sum_of(
                Purchase.objects.filter(
                    supplier=OuterRef("pk"), status=received
                ),
                "supplier",
            ),
            _paid=sum_of(
                PurchasePayment.objects.filter(
                    purchase__supplier=OuterRef("pk"),
                    purchase__status=received,
                    status=active_payment,
                ),
                "purchase__supplier",
                "amount",
            ),
            _applied=sum_of(
                CreditSettlement.objects.filter(
                    supplier=OuterRef("pk"),
                    kind=CreditSettlement.Kind.ADJUSTMENT,
                    purchase__status=received,
                    status=active_credit,
                ),
                "supplier",
                "amount",
            ),
            advance_paid=sum_of(
                PurchasePayment.objects.filter(
                    purchase__supplier=OuterRef("pk"),
                    purchase__status=Purchase.Status.DRAFT,
                    status=active_payment,
                ),
                "purchase__supplier",
                "amount",
            ),
            _credit_earned=sum_of(
                PurchaseReturn.objects.filter(
                    supplier=OuterRef("pk"),
                    status=PurchaseReturn.Status.ACTIVE,
                ),
                "supplier",
                "credit_amount",
            ),
            _credit_used=sum_of(
                CreditSettlement.objects.filter(
                    supplier=OuterRef("pk"), status=active_credit
                ),
                "supplier",
                "amount",
            ),
        ).annotate(
            outstanding=ExpressionWrapper(
                F("total_purchased") - F("_paid") - F("_applied"),
                output_field=MONEY_FIELD,
            ),
            credit_balance=ExpressionWrapper(
                F("_credit_earned") - F("_credit_used"),
                output_field=MONEY_FIELD,
            ),
        )

    def perform_create(self, serializer):
        serializer.save(business=self.request.active_business)

    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        # Re-read so the money figures are present (all zero when new).
        supplier = self.get_queryset().get(pk=response.data["id"])

        return Response(
            self.get_serializer(supplier).data, status=status.HTTP_201_CREATED
        )

    def destroy(self, request, *args, **kwargs):
        supplier = self.get_object()

        if supplier.is_active:
            supplier.is_active = False
            supplier.save(update_fields=["is_active", "updated_at"])

        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Purchases
# ---------------------------------------------------------------------------

class PurchaseViewSet(PurchaseBaseViewSet):
    """
    Purchases of the active business.

    A purchase is a draft until the goods arrive: ``receive`` then adds the
    lines to stock, once, and locks it. Only drafts can be edited or
    cancelled. Filters: ``status``, ``supplier``, ``payment_status``
    (``unpaid``, ``partial``, ``paid``), ``purchase_date__gte`` / ``__lte``.
    """

    http_method_names = ["get", "post", "patch", "head", "options"]

    filterset_fields = {
        "status": ["exact"],
        "supplier": ["exact"],
        "purchase_date": ["gte", "lte"],
    }
    search_fields = ("bill_no", "supplier__name")
    ordering_fields = ("purchase_date", "number", "net_total", "created_at")
    ordering = ("-purchase_date", "-number")

    def get_serializer_class(self):
        if self.action == "retrieve":
            return PurchaseDetailSerializer

        return PurchaseSerializer

    def get_queryset(self):
        paid = sum_of(
            PurchasePayment.objects.filter(
                purchase=OuterRef("pk"), status=PurchasePayment.Status.ACTIVE
            ),
            "purchase",
            "amount",
        )
        credit = sum_of(
            CreditSettlement.objects.filter(
                purchase=OuterRef("pk"),
                kind=CreditSettlement.Kind.ADJUSTMENT,
                status=CreditSettlement.Status.ACTIVE,
            ),
            "purchase",
            "amount",
        )

        items = PurchaseItem.objects.select_related("product").annotate(
            returned=Coalesce(
                Sum(
                    "return_items__quantity",
                    filter=Q(
                        return_items__purchase_return__status=PurchaseReturn.Status.ACTIVE
                    ),
                ),
                Value(
                    Decimal("0.000"),
                    output_field=DecimalField(max_digits=14, decimal_places=3),
                ),
            )
        )

        queryset = (
            Purchase.objects.filter(business=self.request.active_business)
            .select_related("supplier")
            .prefetch_related(Prefetch("items", queryset=items), "payments")
            .annotate(paid=paid, credit=credit)
            .annotate(
                settled=ExpressionWrapper(
                    F("paid") + F("credit"), output_field=MONEY_FIELD
                )
            )
        )

        wanted = self.request.query_params.get("payment_status")

        if wanted == "unpaid":
            queryset = queryset.filter(settled=0)
        elif wanted == "partial":
            queryset = queryset.filter(settled__gt=0, settled__lt=F("net_total"))
        elif wanted == "paid":
            queryset = queryset.filter(settled__gte=F("net_total"))

        return queryset

    def _respond(self, purchase, code=status.HTTP_200_OK):
        fresh = self.get_queryset().get(pk=purchase.pk)

        return Response(PurchaseDetailSerializer(fresh).data, status=code)

    def create(self, request, *args, **kwargs):
        serializer = PurchaseWriteSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)

        data = dict(serializer.validated_data)
        data.setdefault("purchase_date", timezone.localdate())

        purchase = create_purchase(
            business=request.active_business, user=request.user, **data
        )

        return self._respond(purchase, status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        purchase = self.get_object()

        serializer = PurchasePatchSerializer(
            purchase,
            data=request.data,
            partial=True,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)

        purchase = update_purchase(
            purchase=purchase, **serializer.validated_data
        )

        return self._respond(purchase)

    @action(detail=True, methods=["post"])
    def receive(self, request, pk=None):
        """The goods arrived: add every line to stock and lock the purchase."""
        purchase = receive_purchase(
            purchase=self.get_object(), user=request.user
        )

        return self._respond(purchase)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Cancel a draft that is no longer needed, with a reason."""
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        purchase = cancel_purchase(
            purchase=self.get_object(),
            user=request.user,
            reason=serializer.validated_data["reason"],
        )

        return self._respond(purchase)


# ---------------------------------------------------------------------------
# Supplier payments
# ---------------------------------------------------------------------------

class PurchasePaymentViewSet(PurchaseBaseViewSet):
    """
    Payments to suppliers: full or part, also before the goods arrive.
    A payment cannot be edited; void it with a reason. Each payment also
    appears as a ``purchase`` expense.
    """

    serializer_class = PurchasePaymentSerializer
    http_method_names = ["get", "post", "head", "options"]

    filterset_fields = {
        "status": ["exact"],
        "purchase": ["exact"],
        "purchase__supplier": ["exact"],
        "method": ["exact"],
        "paid_on": ["gte", "lte"],
    }
    search_fields = ("reference", "purchase__supplier__name", "purchase__bill_no")
    ordering_fields = ("paid_on", "amount", "created_at")
    ordering = ("-paid_on", "-id")

    def get_queryset(self):
        return PurchasePayment.objects.filter(
            purchase__business=self.request.active_business
        ).select_related("purchase", "purchase__supplier")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        payment = create_payment(
            purchase=data["purchase"],
            user=request.user,
            amount=data["amount"],
            paid_on=data.get("paid_on", timezone.localdate()),
            method=data["method"],
            reference=data.get("reference", ""),
            notes=data.get("notes", ""),
        )

        return Response(
            self.get_serializer(payment).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        payment = void_payment(
            payment=self.get_object(),
            user=request.user,
            reason=serializer.validated_data["reason"],
        )

        return Response(self.get_serializer(payment).data)


# ---------------------------------------------------------------------------
# Returns to the supplier
# ---------------------------------------------------------------------------

class PurchaseReturnViewSet(PurchaseBaseViewSet):
    """
    Goods sent back to the supplier from a received purchase. A return takes
    the goods out of stock and creates a credit with the supplier (shown
    separately as ``credit_balance`` on the supplier). A return cannot be
    edited or deleted; void it with a reason.
    """

    serializer_class = PurchaseReturnSerializer
    http_method_names = ["get", "post", "head", "options"]

    filterset_fields = {
        "status": ["exact"],
        "purchase": ["exact"],
        "supplier": ["exact"],
        "return_date": ["gte", "lte"],
    }
    search_fields = ("reason", "supplier__name", "purchase__bill_no")
    ordering_fields = ("return_date", "number", "credit_amount", "created_at")
    ordering = ("-return_date", "-number")

    def get_queryset(self):
        return (
            PurchaseReturn.objects.filter(business=self.request.active_business)
            .select_related("purchase", "supplier")
            .prefetch_related(
                "items__product", "items__purchase_item"
            )
        )

    def create(self, request, *args, **kwargs):
        serializer = PurchaseReturnWriteSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        ret = create_return(
            purchase=data["purchase"],
            user=request.user,
            return_date=data.get("return_date", timezone.localdate()),
            reason=data["reason"],
            notes=data.get("notes", ""),
            items=data["items"],
        )

        return Response(
            self.get_serializer(self.get_queryset().get(pk=ret.pk)).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        ret = void_return(
            purchase_return=self.get_object(),
            user=request.user,
            reason=serializer.validated_data["reason"],
        )

        return Response(self.get_serializer(self.get_queryset().get(pk=ret.pk)).data)


# ---------------------------------------------------------------------------
# Using the credit
# ---------------------------------------------------------------------------

class CreditSettlementViewSet(PurchaseBaseViewSet):
    """
    Using a supplier's credit: ``refund`` (the supplier paid money back) or
    ``adjustment`` (applied to a purchase so it owes less; this is also how
    replacement goods are paid for). Cannot be edited; void with a reason.
    """

    serializer_class = CreditSettlementSerializer
    http_method_names = ["get", "post", "head", "options"]

    filterset_fields = {
        "status": ["exact"],
        "supplier": ["exact"],
        "kind": ["exact"],
        "purchase": ["exact"],
        "settled_on": ["gte", "lte"],
    }
    search_fields = ("reference", "supplier__name")
    ordering_fields = ("settled_on", "amount", "created_at")
    ordering = ("-settled_on", "-id")

    def get_queryset(self):
        return CreditSettlement.objects.filter(
            supplier__business=self.request.active_business
        ).select_related("supplier", "purchase")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        settlement = create_settlement(
            supplier=data["supplier"],
            user=request.user,
            kind=data["kind"],
            amount=data["amount"],
            settled_on=data.get("settled_on", timezone.localdate()),
            purchase=data.get("purchase"),
            method=data.get("method", ""),
            reference=data.get("reference", ""),
            notes=data.get("notes", ""),
        )

        return Response(
            self.get_serializer(settlement).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        settlement = void_settlement(
            settlement=self.get_object(),
            user=request.user,
            reason=serializer.validated_data["reason"],
        )

        return Response(self.get_serializer(settlement).data)
