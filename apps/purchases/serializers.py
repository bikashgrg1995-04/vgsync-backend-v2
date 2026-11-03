from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from apps.inventory.services.products import get_products_for_business

from .models import (
    CreditSettlement,
    CreditSettlementItem,
    Purchase,
    PurchaseItem,
    PurchasePayment,
    PurchaseReceipt,
    PurchaseReceiptItem,
    PurchaseReturn,
    PurchaseReturnItem,
    Supplier,
)
from .services import (
    calculate_totals,
    credit_applied_total,
    paid_total,
    payment_status,
)

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
# Suppliers
# ---------------------------------------------------------------------------

class SupplierSerializer(serializers.ModelSerializer):
    # Filled in by the view from one aggregate query; see ``SupplierViewSet``.
    total_purchased = serializers.DecimalField(
        **MONEY, read_only=True, default=Decimal("0.00")
    )
    outstanding = serializers.DecimalField(
        **MONEY, read_only=True, default=Decimal("0.00")
    )
    advance_paid = serializers.DecimalField(
        **MONEY, read_only=True, default=Decimal("0.00")
    )
    credit_to_pay = serializers.DecimalField(
        **MONEY, read_only=True, default=Decimal("0.00")
    )
    to_pay_on_order = serializers.DecimalField(
        **MONEY, read_only=True, default=Decimal("0.00")
    )
    credit_to_receive = serializers.DecimalField(
        **MONEY, read_only=True, default=Decimal("0.00")
    )
    stock_to_receive = serializers.DecimalField(
        max_digits=14, decimal_places=3, read_only=True, default=Decimal("0")
    )
    # Quantity still to come on purchases that arrived in parts.
    stock_pending_delivery = serializers.DecimalField(
        max_digits=14, decimal_places=3, read_only=True, default=Decimal("0")
    )

    class Meta:
        model = Supplier
        fields = (
            "id",
            "name",
            "contact_person",
            "phone",
            "email",
            "address",
            "pan_number",
            "notes",
            "is_active",
            "total_purchased",
            "outstanding",
            "advance_paid",
            "credit_to_pay",
            "to_pay_on_order",
            "credit_to_receive",
            "stock_to_receive",
            "stock_pending_delivery",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_name(self, value):
        value = value.strip()

        taken = Supplier.objects.filter(
            business=active_business(self), name__iexact=value
        )

        if self.instance:
            taken = taken.exclude(pk=self.instance.pk)

        if taken.exists():
            raise serializers.ValidationError(
                "A supplier with this name already exists."
            )

        return value


# ---------------------------------------------------------------------------
# Purchases: input
# ---------------------------------------------------------------------------

class CatalogProductField(serializers.PrimaryKeyRelatedField):
    """Only active products of the business's own catalog can be chosen."""

    default_error_messages = {
        "does_not_exist": "Choose an active product from your catalog.",
    }

    def get_queryset(self):
        return get_products_for_business(self.context["request"].active_business)


class PurchaseItemInputSerializer(serializers.Serializer):
    product = CatalogProductField()
    quantity = serializers.DecimalField(**QUANTITY)
    unit_price = serializers.DecimalField(**POSITIVE)


class PurchaseWriteSerializer(serializers.Serializer):
    """
    Create or edit a draft purchase. Totals are never accepted from the
    client: send the lines, ``discount_amount``, ``vat_rate`` and
    ``freight_amount``, and the server calculates everything else.
    """

    supplier = serializers.PrimaryKeyRelatedField(queryset=Supplier.objects.none())
    bill_no = serializers.CharField(max_length=60, required=False, allow_blank=True)
    purchase_date = serializers.DateField(required=False)
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
    freight_amount = serializers.DecimalField(
        **MONEY,
        min_value=Decimal("0"),
        required=False,
        help_text="Transport or transit charge. Not taxed.",
    )
    notes = serializers.CharField(required=False, allow_blank=True)
    items = PurchaseItemInputSerializer(many=True, allow_empty=False, max_length=200)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        request = self.context.get("request")

        if request is not None:
            self.fields["supplier"].queryset = Supplier.objects.filter(
                business=request.active_business, is_active=True
            )
            self.fields["supplier"].error_messages["does_not_exist"] = (
                "Choose an active supplier of this business."
            )

    def validate_purchase_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("The date cannot be in the future.")

        return value

    def validate(self, attrs):
        instance = self.instance

        # Every rule that needs the figures runs on the same calculation the
        # service will save.
        items = attrs.get("items")

        if items is not None:
            lines = [(i["quantity"], i["unit_price"]) for i in items]
        else:
            lines = [(i.quantity, i.unit_price) for i in instance.items.all()]

        calculate_totals(
            lines,
            attrs.get("discount_amount", instance.discount_amount if instance else 0),
            attrs.get("vat_rate", instance.vat_rate if instance else 0),
            attrs.get("freight_amount", instance.freight_amount if instance else 0),
        )

        # The same supplier bill cannot be entered twice.
        bill_no = attrs.get("bill_no", instance.bill_no if instance else "")
        supplier = attrs.get("supplier", instance.supplier if instance else None)

        if bill_no and supplier:
            clash = Purchase.objects.filter(
                business=active_business(self),
                supplier=supplier,
                bill_no=bill_no,
            ).exclude(status=Purchase.Status.CANCELLED)

            if instance:
                clash = clash.exclude(pk=instance.pk)

            if clash.exists():
                raise serializers.ValidationError({
                    "bill_no": "This supplier bill is already entered."
                })

        if instance is None and "items" not in attrs:
            raise serializers.ValidationError({"items": "Add at least one item."})

        return attrs


class PurchasePatchSerializer(PurchaseWriteSerializer):
    """Partial edit: every field optional."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for field in self.fields.values():
            field.required = False


# ---------------------------------------------------------------------------
# Purchases: output
# ---------------------------------------------------------------------------

class PurchaseItemSerializer(serializers.ModelSerializer):
    stock_no = serializers.CharField(source="product.stock_no", read_only=True)
    product_name = serializers.CharField(source="product.name", read_only=True)
    unit = serializers.CharField(source="product.unit", read_only=True)
    returned_quantity = serializers.SerializerMethodField()
    returnable_quantity = serializers.SerializerMethodField()
    remaining_quantity = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseItem
        fields = (
            "id",
            "product",
            "stock_no",
            "product_name",
            "unit",
            "quantity",
            "unit_price",
            "line_total",
            "received_quantity",
            "remaining_quantity",
            "returned_quantity",
            "returnable_quantity",
        )

    def _returned(self, obj):
        # The view annotates this; fall back to the database otherwise.
        annotated = getattr(obj, "returned", None)

        if annotated is not None:
            return annotated

        from django.db.models import Sum

        return (
            obj.return_items.filter(
                purchase_return__status=PurchaseReturn.Status.ACTIVE
            ).aggregate(total=Sum("quantity"))["total"]
            or Decimal("0")
        )

    def get_returned_quantity(self, obj):
        return f"{self._returned(obj):.3f}"

    def get_returnable_quantity(self, obj):
        # Only goods that arrived can be sent back.
        return f"{obj.received_quantity - self._returned(obj):.3f}"

    def get_remaining_quantity(self, obj):
        return f"{obj.quantity - obj.received_quantity:.3f}"


class PurchasePaymentSerializer(serializers.ModelSerializer):
    purchase = serializers.PrimaryKeyRelatedField(queryset=Purchase.objects.none())
    purchase_no = serializers.CharField(source="purchase.purchase_no", read_only=True)
    amount = serializers.DecimalField(**POSITIVE)
    paid_on = serializers.DateField(required=False)

    class Meta:
        model = PurchasePayment
        fields = (
            "id",
            "purchase",
            "purchase_no",
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
            self.fields["purchase"].queryset = Purchase.objects.filter(
                business=request.active_business
            )

    def validate_paid_on(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("Payment date cannot be in the future.")

        return value


class PurchaseSerializer(serializers.ModelSerializer):
    purchase_no = serializers.CharField(read_only=True)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True)
    items = PurchaseItemSerializer(many=True, read_only=True)

    paid_amount = serializers.SerializerMethodField()
    credit_applied = serializers.SerializerMethodField()
    remaining_amount = serializers.SerializerMethodField()
    payment_status = serializers.SerializerMethodField()

    class Meta:
        model = Purchase
        fields = (
            "id",
            "purchase_no",
            "number",
            "supplier",
            "supplier_name",
            "bill_no",
            "purchase_date",
            "status",
            "items",
            "subtotal",
            "discount_amount",
            "vat_rate",
            "vat_amount",
            "freight_amount",
            "net_total",
            "paid_amount",
            "credit_applied",
            "remaining_amount",
            "payment_status",
            "notes",
            "received_at",
            "cancelled_at",
            "cancel_reason",
            "created_at",
            "updated_at",
        )

    def _paid(self, obj):
        # The list view annotates this; a single object asks the database.
        annotated = getattr(obj, "paid", None)

        return annotated if annotated is not None else paid_total(obj)

    def _credit(self, obj):
        annotated = getattr(obj, "credit", None)

        return annotated if annotated is not None else credit_applied_total(obj)

    def get_paid_amount(self, obj):
        return f"{self._paid(obj):.2f}"

    def get_credit_applied(self, obj):
        return f"{self._credit(obj):.2f}"

    def get_remaining_amount(self, obj):
        return f"{obj.net_total - self._paid(obj) - self._credit(obj):.2f}"

    def get_payment_status(self, obj):
        return payment_status(obj.net_total, self._paid(obj) + self._credit(obj))


class PurchaseReceiptItemSerializer(serializers.ModelSerializer):
    product = serializers.IntegerField(source="purchase_item.product_id", read_only=True)
    product_name = serializers.CharField(
        source="purchase_item.product.name", read_only=True
    )

    class Meta:
        model = PurchaseReceiptItem
        fields = ("id", "purchase_item", "product", "product_name", "quantity")


class PurchaseReceiptSerializer(serializers.ModelSerializer):
    items = PurchaseReceiptItemSerializer(many=True, read_only=True)

    class Meta:
        model = PurchaseReceipt
        fields = ("id", "received_on", "notes", "items", "created_at")


class PurchaseDetailSerializer(PurchaseSerializer):
    payments = PurchasePaymentSerializer(many=True, read_only=True)
    receipts = PurchaseReceiptSerializer(many=True, read_only=True)

    class Meta(PurchaseSerializer.Meta):
        fields = PurchaseSerializer.Meta.fields + ("payments", "receipts")


class PurchaseLineField(serializers.PrimaryKeyRelatedField):
    """A line of the purchase being received (taken from the context)."""

    def get_queryset(self):
        purchase = self.context.get("purchase")

        if purchase is None:
            return PurchaseItem.objects.none()

        return purchase.items.all()


class ReceiveItemSerializer(serializers.Serializer):
    purchase_item = PurchaseLineField()
    quantity = serializers.DecimalField(
        max_digits=14, decimal_places=3, min_value=Decimal("0.001")
    )


class ReceivePurchaseSerializer(serializers.Serializer):
    """All fields are optional: no ``items`` means everything still to come."""

    items = ReceiveItemSerializer(many=True, required=False, allow_empty=False)
    received_on = serializers.DateField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate_received_on(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("The date cannot be in the future.")

        return value


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(allow_blank=False, trim_whitespace=True)


# ---------------------------------------------------------------------------
# Returns to the supplier
# ---------------------------------------------------------------------------

class PurchaseItemField(serializers.PrimaryKeyRelatedField):
    """A line of a purchase of the active business."""

    default_error_messages = {
        "does_not_exist": "This purchase line does not exist.",
    }

    def get_queryset(self):
        return PurchaseItem.objects.filter(
            purchase__business=self.context["request"].active_business
        ).select_related("product")


class PurchaseReturnItemInputSerializer(serializers.Serializer):
    purchase_item = PurchaseItemField()
    quantity = serializers.DecimalField(**QUANTITY)
    stock_condition = serializers.ChoiceField(
        choices=[("good", "Good"), ("damaged", "Damaged")],
        default="good",
        help_text="Which stock the goods are taken from.",
    )


class PurchaseReturnWriteSerializer(serializers.Serializer):
    purchase = serializers.PrimaryKeyRelatedField(queryset=Purchase.objects.none())
    return_date = serializers.DateField(required=False)
    reason = serializers.CharField(allow_blank=False, trim_whitespace=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    items = PurchaseReturnItemInputSerializer(many=True, allow_empty=False, max_length=200)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        request = self.context.get("request")

        if request is not None:
            self.fields["purchase"].queryset = Purchase.objects.filter(
                business=request.active_business
            )

    def validate_return_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("The date cannot be in the future.")

        return value


class PurchaseReturnItemSerializer(serializers.ModelSerializer):
    stock_no = serializers.CharField(source="product.stock_no", read_only=True)
    product_name = serializers.CharField(source="product.name", read_only=True)
    unit_price = serializers.DecimalField(
        source="purchase_item.unit_price", max_digits=12, decimal_places=2, read_only=True
    )

    class Meta:
        model = PurchaseReturnItem
        fields = (
            "id",
            "purchase_item",
            "product",
            "stock_no",
            "product_name",
            "quantity",
            "unit_price",
            "stock_condition",
            "credit_amount",
        )


class PurchaseReturnSerializer(serializers.ModelSerializer):
    return_no = serializers.CharField(read_only=True)
    purchase_no = serializers.CharField(source="purchase.purchase_no", read_only=True)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True)
    items = PurchaseReturnItemSerializer(many=True, read_only=True)

    class Meta:
        model = PurchaseReturn
        fields = (
            "id",
            "return_no",
            "number",
            "purchase",
            "purchase_no",
            "supplier",
            "supplier_name",
            "return_date",
            "reason",
            "notes",
            "credit_amount",
            "status",
            "voided_at",
            "void_reason",
            "items",
            "created_at",
        )


# ---------------------------------------------------------------------------
# Using the credit
# ---------------------------------------------------------------------------

class ReceiveReplacementSerializer(serializers.Serializer):
    received_on = serializers.DateField(required=False)

    def validate_received_on(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("The date cannot be in the future.")

        return value


class CreditSettlementItemSerializer(serializers.ModelSerializer):
    product = CatalogProductField()
    product_name = serializers.CharField(source="product.name", read_only=True)
    quantity = serializers.DecimalField(**QUANTITY)

    class Meta:
        model = CreditSettlementItem
        fields = ("id", "product", "product_name", "quantity")
        read_only_fields = ("id",)


class CreditSettlementSerializer(serializers.ModelSerializer):
    supplier = serializers.PrimaryKeyRelatedField(queryset=Supplier.objects.none())
    supplier_name = serializers.CharField(source="supplier.name", read_only=True)
    purchase = serializers.PrimaryKeyRelatedField(
        queryset=Purchase.objects.none(), required=False, allow_null=True
    )
    purchase_no = serializers.SerializerMethodField()
    kind = serializers.ChoiceField(choices=CreditSettlement.Kind.choices)
    amount = serializers.DecimalField(**POSITIVE)
    settled_on = serializers.DateField(required=False)
    method = serializers.ChoiceField(
        choices=PurchasePayment.Method.choices, required=False, allow_blank=True
    )
    items = CreditSettlementItemSerializer(many=True, required=False)
    delivery_status = serializers.SerializerMethodField()

    class Meta:
        model = CreditSettlement
        fields = (
            "id",
            "supplier",
            "supplier_name",
            "kind",
            "amount",
            "settled_on",
            "purchase",
            "purchase_no",
            "method",
            "items",
            "delivery_status",
            "received_on",
            "reference",
            "notes",
            "status",
            "voided_at",
            "void_reason",
            "created_at",
        )
        read_only_fields = (
            "id", "status", "received_on", "voided_at", "void_reason", "created_at",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        request = self.context.get("request")

        if request is not None:
            business = request.active_business
            self.fields["supplier"].queryset = Supplier.objects.filter(business=business)
            self.fields["purchase"].queryset = Purchase.objects.filter(business=business)

    def get_purchase_no(self, obj):
        return obj.purchase.purchase_no if obj.purchase_id else None

    def get_delivery_status(self, obj):
        """``pending`` while the replacement goods are still to arrive."""
        if obj.kind != CreditSettlement.Kind.REPLACEMENT:
            return None

        return "received" if obj.received_on else "pending"

    def validate_settled_on(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("The date cannot be in the future.")

        return value

    def validate(self, attrs):
        kind = attrs["kind"]

        if kind == CreditSettlement.Kind.ADJUSTMENT:
            if not attrs.get("purchase"):
                raise serializers.ValidationError(
                    {"purchase": "Choose the purchase to apply the credit to."}
                )

            if attrs.get("method"):
                raise serializers.ValidationError(
                    {"method": "A method applies to refunds only."}
                )

        elif kind == CreditSettlement.Kind.REPLACEMENT:
            if attrs.get("purchase"):
                raise serializers.ValidationError(
                    {"purchase": "A replacement is not applied to a purchase."}
                )

            if attrs.get("method"):
                raise serializers.ValidationError(
                    {"method": "A method applies to refunds only."}
                )

            if not attrs.get("items"):
                raise serializers.ValidationError(
                    {"items": "List the goods the supplier will send."}
                )

        else:
            if attrs.get("purchase"):
                raise serializers.ValidationError(
                    {"purchase": "A refund is not applied to a purchase."}
                )

            if not attrs.get("method"):
                raise serializers.ValidationError(
                    {"method": "Say how the refund was received."}
                )

        return attrs
