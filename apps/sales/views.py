"""
Sales APIs, scoped to the active business and to the ``sales`` module
permission. Writes need full access to the business.
"""

from decimal import Decimal

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import ExpressionWrapper, F, OuterRef, Prefetch
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.permissions import HasActiveBusinessContext, HasModulePermission
from apps.purchases.views import MONEY_FIELD, sum_of

from .models import BikeUnit, Customer, Followup, Sale, SaleItem, SalePayment
from .serializers import (
    BikeUnitSerializer,
    CustomerSerializer,
    FollowupDoneSerializer,
    FollowupSerializer,
    FollowupSkipSerializer,
    ReasonSerializer,
    SaleDetailSerializer,
    SalePatchSerializer,
    SalePaymentSerializer,
    SaleSerializer,
    SaleWriteSerializer,
)
from .services import (
    cancel_sale,
    complete_followup,
    confirm_sale,
    create_payment,
    create_sale,
    register_bike_unit,
    skip_followup,
    update_sale,
    void_payment,
)


class SalesBaseViewSet(viewsets.ModelViewSet):
    permission_classes = [
        IsAuthenticated,
        HasActiveBusinessContext,
        HasModulePermission,
    ]
    permission_module = "sales"

    def handle_exception(self, exc):
        # The inventory services raise Django errors (for example "not
        # enough stock"); show them as proper API errors.
        if isinstance(exc, DjangoValidationError):
            exc = ValidationError(detail=exc.messages)

        elif isinstance(exc, DjangoPermissionDenied):
            exc = PermissionDenied(detail=str(exc))

        return super().handle_exception(exc)


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

class CustomerViewSet(SalesBaseViewSet):
    """
    Customers of the active business, with what they owe.
    ``DELETE`` deactivates; a customer with sales is never removed.

    ``total_sales`` and ``credit_to_receive`` count what the customer has to
    pay in all on confirmed sales, including any EMI finance charge; so
    ``credit_to_receive`` is everything still owed, instalments not yet due
    included.
    ``advance_received`` is money taken on drafts that are not yet confirmed.
    """

    serializer_class = CustomerSerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    filterset_fields = ("is_active",)
    search_fields = ("name", "phone", "pan_number")
    ordering_fields = ("name", "created_at", "credit_to_receive")
    ordering = ("name",)

    def get_queryset(self):
        active = SalePayment.Status.ACTIVE
        confirmed = Sale.Status.CONFIRMED

        return Customer.objects.filter(
            business=self.request.active_business
        ).annotate(
            _net=sum_of(
                Sale.objects.filter(customer=OuterRef("pk"), status=confirmed),
                "customer",
            ),
            _finance=sum_of(
                Sale.objects.filter(customer=OuterRef("pk"), status=confirmed),
                "customer",
                "finance_charge",
            ),
            _received=sum_of(
                SalePayment.objects.filter(
                    sale__customer=OuterRef("pk"),
                    sale__status=confirmed,
                    status=active,
                ),
                "sale__customer",
                "amount",
            ),
            advance_received=sum_of(
                SalePayment.objects.filter(
                    sale__customer=OuterRef("pk"),
                    sale__status=Sale.Status.DRAFT,
                    status=active,
                ),
                "sale__customer",
                "amount",
            ),
        ).annotate(
            total_sales=ExpressionWrapper(
                F("_net") + F("_finance"), output_field=MONEY_FIELD
            )
        ).annotate(
            credit_to_receive=ExpressionWrapper(
                F("total_sales") - F("_received"), output_field=MONEY_FIELD
            )
        )

    def perform_create(self, serializer):
        serializer.save(business=self.request.active_business)

    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        # Re-read so the money figures are present (all zero when new).
        customer = self.get_queryset().get(pk=response.data["id"])

        return Response(
            self.get_serializer(customer).data, status=status.HTTP_201_CREATED
        )

    def destroy(self, request, *args, **kwargs):
        customer = self.get_object()

        if customer.is_active:
            customer.is_active = False
            customer.save(update_fields=["is_active", "updated_at"])

        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------

class SaleViewSet(SalesBaseViewSet):
    """
    Sales of the active business.

    A sale is a draft until it is confirmed: ``confirm`` then takes the lines
    out of stock, gives the invoice number and locks it. Only drafts can be
    edited or cancelled. Filters: ``status``, ``customer``,
    ``sale_type`` (``stock``, ``bike``, ``maintenance``), ``payment_mode``
    (``full``, ``emi``), ``payment_status`` (``unpaid``, ``partial``,
    ``paid``), ``sale_date__gte`` / ``__lte``.
    """

    http_method_names = ["get", "post", "patch", "head", "options"]

    filterset_fields = {
        "status": ["exact"],
        "sale_type": ["exact"],
        "payment_mode": ["exact"],
        "customer": ["exact"],
        "sale_date": ["gte", "lte"],
    }
    search_fields = (
        "customer__name",
        "walk_in_name",
        "customer__phone",
        "vehicle_chassis_no",
        "vehicle_reg_no",
        "items__bike_unit__chassis_no",
    )
    ordering_fields = ("sale_date", "number", "net_total", "created_at")
    ordering = ("-sale_date", "-number")

    def get_serializer_class(self):
        if self.action == "retrieve":
            return SaleDetailSerializer

        return SaleSerializer

    def get_queryset(self):
        paid = sum_of(
            SalePayment.objects.filter(
                sale=OuterRef("pk"), status=SalePayment.Status.ACTIVE
            ),
            "sale",
            "amount",
        )

        queryset = (
            Sale.objects.filter(business=self.request.active_business)
            .select_related("customer")
            .prefetch_related(
                Prefetch("items", queryset=SaleItem.objects.select_related("product")),
                "payments",
            )
            .annotate(paid=paid)
            .annotate(
                due=ExpressionWrapper(
                    F("net_total") + F("finance_charge"), output_field=MONEY_FIELD
                )
            )
        )

        wanted = self.request.query_params.get("payment_status")

        if wanted == "unpaid":
            queryset = queryset.filter(paid=0)
        elif wanted == "partial":
            queryset = queryset.filter(paid__gt=0, paid__lt=F("due"))
        elif wanted == "paid":
            queryset = queryset.filter(paid__gte=F("due"))

        return queryset

    def _respond(self, sale, code=status.HTTP_200_OK):
        fresh = self.get_queryset().get(pk=sale.pk)

        return Response(SaleDetailSerializer(fresh).data, status=code)

    def create(self, request, *args, **kwargs):
        serializer = SaleWriteSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)

        data = dict(serializer.validated_data)
        data.setdefault("sale_date", timezone.localdate())

        sale = create_sale(
            business=request.active_business, user=request.user, **data
        )

        return self._respond(sale, status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        sale = self.get_object()

        serializer = SalePatchSerializer(
            sale,
            data=request.data,
            partial=True,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)

        sale = update_sale(sale=sale, **serializer.validated_data)

        return self._respond(sale)

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        """Hand over the goods: take every line out of stock and lock the sale."""
        sale = confirm_sale(sale=self.get_object(), user=request.user)

        return self._respond(sale)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Cancel a draft that is no longer needed, with a reason."""
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        sale = cancel_sale(
            sale=self.get_object(),
            user=request.user,
            reason=serializer.validated_data["reason"],
        )

        return self._respond(sale)


# ---------------------------------------------------------------------------
# Customer payments
# ---------------------------------------------------------------------------

class SalePaymentViewSet(SalesBaseViewSet):
    """
    Money received from customers: full or part, also before the sale is
    confirmed. A payment cannot be edited; void it with a reason.
    """

    serializer_class = SalePaymentSerializer
    http_method_names = ["get", "post", "head", "options"]

    filterset_fields = {
        "status": ["exact"],
        "sale": ["exact"],
        "sale__customer": ["exact"],
        "method": ["exact"],
        "paid_on": ["gte", "lte"],
    }
    search_fields = ("reference", "sale__customer__name", "sale__walk_in_name")
    ordering_fields = ("paid_on", "amount", "created_at")
    ordering = ("-paid_on", "-id")

    def get_queryset(self):
        return SalePayment.objects.filter(
            sale__business=self.request.active_business
        ).select_related("sale", "sale__customer")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        payment = create_payment(
            sale=data["sale"],
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
# Bike units
# ---------------------------------------------------------------------------

class BikeUnitViewSet(SalesBaseViewSet):
    """
    The bikes of the active business, one record per chassis number. A unit
    is in stock until a bike sale is confirmed. Units are never deleted.
    """

    serializer_class = BikeUnitSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    filterset_fields = ("status", "product")
    search_fields = ("chassis_no", "engine_no", "color", "product__name")
    ordering_fields = ("created_at", "chassis_no")
    ordering = ("-id",)

    def get_queryset(self):
        return BikeUnit.objects.filter(
            business=self.request.active_business
        ).select_related("product")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)

        unit = register_bike_unit(
            business=request.active_business, product=data.pop("product"), **data
        )

        return Response(
            self.get_serializer(unit).data, status=status.HTTP_201_CREATED
        )


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------

class FollowupViewSet(SalesBaseViewSet):
    """
    Calls and visits to make: maintenance 3 and 90 days after a service
    (counted from the sale date), EMI instalments, and credit still to receive. They are created by the system
    when a sale is confirmed and close by themselves once the money is
    received. Filters: ``kind``, ``status``, ``customer``, ``sale``,
    ``due_date__gte`` / ``__lte``, and ``overdue=true`` (pending and past
    the due date).
    """

    serializer_class = FollowupSerializer
    http_method_names = ["get", "post", "head", "options"]

    filterset_fields = {
        "kind": ["exact"],
        "status": ["exact"],
        "customer": ["exact"],
        "sale": ["exact"],
        "due_date": ["gte", "lte"],
    }
    search_fields = (
        "customer__name",
        "customer__phone",
        "sale__walk_in_name",
        "sale__vehicle_chassis_no",
        "sale__vehicle_reg_no",
    )
    ordering_fields = ("due_date", "created_at")
    ordering = ("due_date", "id")

    def get_queryset(self):
        queryset = Followup.objects.filter(
            business=self.request.active_business
        ).select_related("sale", "customer", "installment")

        if self.request.query_params.get("overdue") in ("true", "1"):
            queryset = queryset.filter(
                status=Followup.Status.PENDING,
                due_date__lt=timezone.localdate(),
            )

        return queryset

    def create(self, request, *args, **kwargs):
        # Follow-ups come from sales, not from the client.
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(detail=True, methods=["post"])
    def done(self, request, pk=None):
        """Mark it done, with a note. Maintenance can record an issue and book the next one."""
        serializer = FollowupDoneSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        followup = complete_followup(
            followup=self.get_object(),
            user=request.user,
            note=serializer.validated_data["note"],
            next_due_date=serializer.validated_data.get("next_due_date"),
            has_issue=serializer.validated_data["has_issue"],
        )

        return Response(self.get_serializer(followup).data)

    @action(detail=True, methods=["post"])
    def skip(self, request, pk=None):
        """Give up on it, with a note saying why."""
        serializer = FollowupSkipSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        followup = skip_followup(
            followup=self.get_object(),
            user=request.user,
            note=serializer.validated_data["note"],
        )

        return Response(self.get_serializer(followup).data)
