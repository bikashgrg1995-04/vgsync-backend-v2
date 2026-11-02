from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from apps.inventory.services.products import get_products_for_business

from .models import Customer, Sale, SaleItem, SalePayment
from .services import calculate_totals, paid_total, payment_status

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


class SaleItemInputSerializer(serializers.Serializer):
    product = CatalogProductField()
    quantity = serializers.DecimalField(**QUANTITY)
    unit_price = serializers.DecimalField(
        **POSITIVE,
        required=False,
        help_text="Leave out to use the product's sale price.",
    )


class SaleWriteSerializer(serializers.Serializer):
    """
    Create or edit a draft sale. Totals are never accepted from the client:
    send the lines, ``discount_amount`` and ``vat_rate``, and the server
    calculates everything else.
    """

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

    def validate(self, attrs):
        instance = self.instance
        items = attrs.get("items")

        # Check the discount against the same figures the service will save.
        if items is not None:
            lines = [
                (i["quantity"], i.get("unit_price") or i["product"].sale_price)
                for i in items
            ]
        else:
            lines = [(i.quantity, i.unit_price) for i in instance.items.all()]

        if all(price for _, price in lines):
            calculate_totals(
                lines,
                attrs.get("discount_amount", instance.discount_amount if instance else 0),
                attrs.get("vat_rate", instance.vat_rate if instance else 0),
            )

        return attrs


class SalePatchSerializer(SaleWriteSerializer):
    """Editing a draft: every field is optional."""

    items = SaleItemInputSerializer(
        many=True, allow_empty=False, max_length=200, required=False
    )


# ---------------------------------------------------------------------------
# Sales: output
# ---------------------------------------------------------------------------

class SaleItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)
    stock_no = serializers.CharField(source="product.stock_no", read_only=True)

    class Meta:
        model = SaleItem
        fields = (
            "id",
            "product",
            "product_name",
            "stock_no",
            "quantity",
            "unit_price",
            "line_total",
        )


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
            "status",
            "items",
            "subtotal",
            "discount_amount",
            "vat_rate",
            "vat_amount",
            "net_total",
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
        return f"{obj.net_total - self._paid(obj):.2f}"

    def get_payment_status(self, obj):
        return payment_status(obj.net_total, self._paid(obj))


class SaleDetailSerializer(SaleSerializer):
    payments = SalePaymentSerializer(many=True, read_only=True)

    class Meta(SaleSerializer.Meta):
        fields = SaleSerializer.Meta.fields + ("payments",)


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(allow_blank=False, trim_whitespace=True)
