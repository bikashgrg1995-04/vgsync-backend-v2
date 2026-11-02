from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from apps.inventory.services.products import get_products_for_business

from .models import BikeUnit, Customer, Followup, Sale, SaleItem, SalePayment
from .services import installment_rows, paid_total, payment_status

MONEY = {"max_digits": 12, "decimal_places": 2}
POSITIVE = {**MONEY, "min_value": Decimal("0.01")}
QUANTITY = {
    "max_digits": 14,
    "decimal_places": 3,
    "min_value": Decimal("0.001"),
}


def active_business(serializer):
    return serializer.context["request"].active_business


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

class CustomerSerializer(serializers.ModelSerializer):
    # Filled in by the view from one aggregate query; see ``CustomerViewSet``.
    total_sales = serializers.DecimalField(
        **MONEY, read_only=True, default=Decimal("0.00")
    )
    credit_to_receive = serializers.DecimalField(
        **MONEY, read_only=True, default=Decimal("0.00")
    )
    advance_received = serializers.DecimalField(
        **MONEY, read_only=True, default=Decimal("0.00")
    )

    class Meta:
        model = Customer
        fields = (
            "id",
            "name",
            "phone",
            "email",
            "address",
            "pan_number",
            "notes",
            "is_active",
            "total_sales",
            "credit_to_receive",
            "advance_received",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_name(self, value):
        value = value.strip()

        if not value:
            raise serializers.ValidationError("This field may not be blank.")

        return value

    def validate_phone(self, value):
        value = value.strip()

        if value:
            taken = Customer.objects.filter(
                business=active_business(self), phone=value
            )

            if self.instance:
                taken = taken.exclude(pk=self.instance.pk)

            if taken.exists():
                raise serializers.ValidationError(
                    "A customer with this phone number already exists."
                )

        return value


# ---------------------------------------------------------------------------
# Sales: input
# ---------------------------------------------------------------------------

class CatalogProductField(serializers.PrimaryKeyRelatedField):
    """Only active products of the business's own catalog can be chosen."""

    default_error_messages = {
        "does_not_exist": "Choose an active product from your catalog.",
    }

    def get_queryset(self):
        return get_products_for_business(self.context["request"].active_business)


class BikeUnitField(serializers.PrimaryKeyRelatedField):
    """A bike unit of the active business."""

    default_error_messages = {
        "does_not_exist": "Choose a bike unit of this business.",
    }

    def get_queryset(self):
        return BikeUnit.objects.filter(
            business=self.context["request"].active_business
        ).select_related("product")


class SaleItemInputSerializer(serializers.Serializer):
    """
    A line. Stock sale: ``product``. Bike sale: ``bike_unit``. Maintenance:
    a ``product`` (a part) or a ``description`` (a service charge).
    """

    product = CatalogProductField(required=False)
    bike_unit = BikeUnitField(required=False)
    description = serializers.CharField(
        max_length=200, required=False, allow_blank=True
    )
    quantity = serializers.DecimalField(**QUANTITY, required=False)
    unit_price = serializers.DecimalField(
        **POSITIVE,
        required=False,
        help_text="Leave out to use the product's sale price.",
    )


class SaleWriteSerializer(serializers.Serializer):
    """
    Create or edit a draft sale. Totals are never accepted from the client:
    send the lines, ``discount_amount`` and ``vat_rate``, and the server
    calculates everything else, including the finance charge of an EMI.
    """

    sale_type = serializers.ChoiceField(
        choices=Sale.SaleType.choices, required=False
    )
    customer = serializers.PrimaryKeyRelatedField(
        queryset=Customer.objects.none(), required=False, allow_null=True
    )
    walk_in_name = serializers.CharField(
        max_length=150, required=False, allow_blank=True
    )
    sale_date = serializers.DateField(required=False)
    discount_amount = serializers.DecimalField(
        **MONEY, min_value=Decimal("0"), required=False
    )
    vat_rate = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=Decimal("0"),
        max_value=Decimal("100"),
        required=False,
        help_text="VAT percent on the discounted amount. Defaults to 0.",
    )
    # Bike sales: full payment (default) or EMI.
    payment_mode = serializers.ChoiceField(
        choices=Sale.PaymentMode.choices, required=False
    )
    down_payment = serializers.DecimalField(
        **MONEY, min_value=Decimal("0"), required=False
    )
    emi_months = serializers.IntegerField(
        min_value=1, max_value=120, required=False, allow_null=True
    )
    emi_amount = serializers.DecimalField(
        **POSITIVE, required=False, allow_null=True,
        help_text="Monthly instalment.",
    )
    first_emi_due = serializers.DateField(
        required=False, allow_null=True,
        help_text="Defaults to one month after the sale date.",
    )
    # Credit sales: when the customer promised to pay the rest.
    due_date = serializers.DateField(required=False, allow_null=True)
    # Maintenance: the customer's bike.
    vehicle_chassis_no = serializers.CharField(
        max_length=60, required=False, allow_blank=True
    )
    vehicle_reg_no = serializers.CharField(
        max_length=30, required=False, allow_blank=True
    )
    notes = serializers.CharField(required=False, allow_blank=True)
    items = SaleItemInputSerializer(many=True, allow_empty=False, max_length=200)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        request = self.context.get("request")

        if request is not None:
            self.fields["customer"].queryset = Customer.objects.filter(
                business=request.active_business, is_active=True
            )
            self.fields["customer"].error_messages["does_not_exist"] = (
                "Choose an active customer of this business."
            )

    def validate_sale_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("The date cannot be in the future.")

        return value


class SalePatchSerializer(SaleWriteSerializer):
    """Editing a draft: every field is optional."""

    items = SaleItemInputSerializer(
        many=True, allow_empty=False, max_length=200, required=False
    )


# ---------------------------------------------------------------------------
# Sales: output
# ---------------------------------------------------------------------------

class SaleItemSerializer(serializers.ModelSerializer):
    product_name = serializers.SerializerMethodField()
    stock_no = serializers.CharField(source="product.stock_no", default="", read_only=True)
    chassis_no = serializers.CharField(source="bike_unit.chassis_no", default=None, read_only=True)
    engine_no = serializers.CharField(source="bike_unit.engine_no", default=None, read_only=True)

    class Meta:
        model = SaleItem
        fields = (
            "id",
            "product",
            "product_name",
            "stock_no",
            "bike_unit",
            "chassis_no",
            "engine_no",
            "description",
            "quantity",
            "unit_price",
            "line_total",
        )

    def get_product_name(self, obj):
        return obj.product.name if obj.product_id else obj.description


class SalePaymentSerializer(serializers.ModelSerializer):
    sale = serializers.PrimaryKeyRelatedField(queryset=Sale.objects.none())
    sale_no = serializers.CharField(source="sale.sale_no", read_only=True)
    amount = serializers.DecimalField(**POSITIVE)
    paid_on = serializers.DateField(required=False)

    class Meta:
        model = SalePayment
        fields = (
            "id",
            "sale",
            "sale_no",
            "amount",
            "paid_on",
            "method",
            "reference",
            "notes",
            "status",
            "voided_at",
            "void_reason",
            "created_at",
        )
        read_only_fields = ("id", "status", "voided_at", "void_reason", "created_at")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        request = self.context.get("request")

        if request is not None:
            self.fields["sale"].queryset = Sale.objects.filter(
                business=request.active_business
            )

    def validate_paid_on(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("Payment date cannot be in the future.")

        return value


class SaleSerializer(serializers.ModelSerializer):
    sale_no = serializers.CharField(read_only=True)
    invoice_no = serializers.CharField(read_only=True)
    customer_name = serializers.SerializerMethodField()
    items = SaleItemSerializer(many=True, read_only=True)

    amount_due = serializers.DecimalField(**MONEY, read_only=True)
    paid_amount = serializers.SerializerMethodField()
    remaining_amount = serializers.SerializerMethodField()
    payment_status = serializers.SerializerMethodField()

    class Meta:
        model = Sale
        fields = (
            "id",
            "sale_no",
            "number",
            "invoice_no",
            "invoice_number",
            "customer",
            "customer_name",
            "walk_in_name",
            "sale_date",
            "sale_type",
            "status",
            "items",
            "subtotal",
            "discount_amount",
            "vat_rate",
            "vat_amount",
            "net_total",
            "payment_mode",
            "down_payment",
            "emi_months",
            "emi_amount",
            "first_emi_due",
            "finance_charge",
            "amount_due",
            "due_date",
            "vehicle_chassis_no",
            "vehicle_reg_no",
            "paid_amount",
            "remaining_amount",
            "payment_status",
            "notes",
            "confirmed_at",
            "cancelled_at",
            "cancel_reason",
            "created_at",
            "updated_at",
        )

    def _paid(self, obj):
        # The list view annotates this; a single object asks the database.
        annotated = getattr(obj, "paid", None)

        return annotated if annotated is not None else paid_total(obj)

    def get_customer_name(self, obj):
        if obj.customer_id:
            return obj.customer.name

        return obj.walk_in_name or "Walk-in"

    def get_paid_amount(self, obj):
        return f"{self._paid(obj):.2f}"

    def get_remaining_amount(self, obj):
        return f"{obj.amount_due - self._paid(obj):.2f}"

    def get_payment_status(self, obj):
        return payment_status(obj.amount_due, self._paid(obj))


class InstallmentSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    number = serializers.IntegerField()
    due_date = serializers.DateField()
    amount = serializers.DecimalField(**MONEY)
    paid_amount = serializers.DecimalField(**MONEY)
    status = serializers.CharField()


class SaleDetailSerializer(SaleSerializer):
    payments = SalePaymentSerializer(many=True, read_only=True)
    installments = serializers.SerializerMethodField()

    class Meta(SaleSerializer.Meta):
        fields = SaleSerializer.Meta.fields + ("payments", "installments")

    def get_installments(self, obj):
        return InstallmentSerializer(installment_rows(obj), many=True).data


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(allow_blank=False, trim_whitespace=True)


# ---------------------------------------------------------------------------
# Bike units
# ---------------------------------------------------------------------------

class BikeUnitSerializer(serializers.ModelSerializer):
    product = CatalogProductField()
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = BikeUnit
        fields = (
            "id",
            "product",
            "product_name",
            "chassis_no",
            "engine_no",
            "color",
            "notes",
            "status",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "status", "created_at", "updated_at")

    def validate_chassis_no(self, value):
        value = value.strip()

        if not value:
            raise serializers.ValidationError("This field may not be blank.")

        taken = BikeUnit.objects.filter(chassis_no__iexact=value)

        if self.instance:
            taken = taken.exclude(pk=self.instance.pk)

        if taken.exists():
            raise serializers.ValidationError(
                "A bike with this chassis number is already registered."
            )

        return value

    def validate_engine_no(self, value):
        value = value.strip()

        if value:
            taken = BikeUnit.objects.filter(engine_no__iexact=value)

            if self.instance:
                taken = taken.exclude(pk=self.instance.pk)

            if taken.exists():
                raise serializers.ValidationError(
                    "A bike with this engine number is already registered."
                )

        return value

    def validate(self, attrs):
        if self.instance is not None:
            if self.instance.status == BikeUnit.Status.SOLD:
                raise serializers.ValidationError(
                    "A sold bike cannot be changed."
                )

            if "product" in attrs and attrs["product"] != self.instance.product:
                raise serializers.ValidationError(
                    {"product": "The model of a registered bike cannot be changed."}
                )

        return attrs


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------

class FollowupSerializer(serializers.ModelSerializer):
    sale_no = serializers.CharField(source="sale.sale_no", read_only=True)
    invoice_no = serializers.CharField(source="sale.invoice_no", read_only=True)
    sale_type = serializers.CharField(source="sale.sale_type", read_only=True)
    customer_name = serializers.SerializerMethodField()
    customer_phone = serializers.CharField(
        source="customer.phone", default="", read_only=True
    )
    vehicle_chassis_no = serializers.CharField(
        source="sale.vehicle_chassis_no", read_only=True
    )
    vehicle_reg_no = serializers.CharField(
        source="sale.vehicle_reg_no", read_only=True
    )
    installment_number = serializers.IntegerField(
        source="installment.number", default=None, read_only=True
    )
    sale_date = serializers.DateField(source="sale.sale_date", read_only=True)
    is_overdue = serializers.SerializerMethodField()
    previous_followup = serializers.SerializerMethodField()

    class Meta:
        model = Followup
        fields = (
            "id",
            "kind",
            "sale",
            "sale_no",
            "invoice_no",
            "sale_type",
            "sale_date",
            "customer",
            "customer_name",
            "customer_phone",
            "vehicle_chassis_no",
            "vehicle_reg_no",
            "installment_number",
            "due_date",
            "amount",
            "status",
            "is_overdue",
            "auto_closed",
            "completed_on",
            "note",
            "has_issue",
            "previous_followup",
            "created_at",
        )

    def get_previous_followup(self, obj):
        """
        On the 90-day follow-up: what the 3-day check found, so the person
        calling already knows whether the customer had a problem.
        """
        if obj.kind != Followup.Kind.MAINTENANCE_DUE:
            return None

        post = Followup.objects.filter(
            sale_id=obj.sale_id, kind=Followup.Kind.MAINTENANCE_POST
        ).first()

        if post is None:
            return None

        return {
            "id": post.id,
            "due_date": post.due_date,
            "status": post.status,
            "has_issue": post.has_issue,
            "note": post.note,
        }

    def get_customer_name(self, obj):
        if obj.customer_id:
            return obj.customer.name

        return obj.sale.walk_in_name or "Walk-in"

    def get_is_overdue(self, obj):
        return (
            obj.status == Followup.Status.PENDING
            and obj.due_date < timezone.localdate()
        )


class FollowupDoneSerializer(serializers.Serializer):
    note = serializers.CharField(
        max_length=300, required=False, allow_blank=True, default=""
    )
    next_due_date = serializers.DateField(required=False)
    has_issue = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Maintenance only: the customer reported a problem.",
    )


class FollowupSkipSerializer(serializers.Serializer):
    note = serializers.CharField(
        max_length=300, allow_blank=False, trim_whitespace=True
    )
