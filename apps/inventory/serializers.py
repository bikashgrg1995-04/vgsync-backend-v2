
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.inventory.models import (
    Product,
    StockBalance,
    StockMovement,
)
from apps.inventory.services.products import get_product_for_business
from apps.inventory.services.stock import validate_quantity


# ==================================================
# PRODUCT
# ==================================================

class ProductSerializer(serializers.ModelSerializer):
    owner_business_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = Product
        fields = [
            "id",
            "owner_business_id",
            "stock_no",
            "name",
            "description",
            "brand",
            "model",
            "category",
            "group",
            "unit",
            "mrp",
            "purchase_price",
            "sale_price",
            "image",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "owner_business_id",
            "is_active",
            "created_at",
            "updated_at",
        ]

    def validate_stock_no(self, value):
        business = self.context["request"].active_business

        queryset = Product.objects.filter(
            owner_business=business,
            stock_no=value,
        )

        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)

        if queryset.exists():
            raise serializers.ValidationError(
                "This stock number already exists in your product catalog."
            )

        return value


# ==================================================
# STOCK BALANCE
# ==================================================

class StockBalanceSerializer(serializers.ModelSerializer):
    product_id = serializers.IntegerField(read_only=True)

    stock_no = serializers.CharField(
        source="product.stock_no",
        read_only=True,
    )

    product_name = serializers.CharField(
        source="product.name",
        read_only=True,
    )

    unit = serializers.CharField(
        source="product.unit",
        read_only=True,
    )

    total_quantity = serializers.SerializerMethodField()

    class Meta:
        model = StockBalance
        fields = [
            "id",
            "product_id",
            "stock_no",
            "product_name",
            "unit",
            "quantity",
            "damaged_quantity",
            "total_quantity",
            "updated_at",
        ]
        read_only_fields = fields

    def get_total_quantity(self, obj):
        return obj.quantity + obj.damaged_quantity


# ==================================================
# BUSINESS STOCK CATALOG RESPONSE
# ==================================================

class BusinessStockSerializer(serializers.ModelSerializer):
    """
    Shows all active catalog products with stock belonging
    to the current business, including zero-stock products.
    """

    product_id = serializers.IntegerField(
        source="pk",
        read_only=True,
    )

    product_name = serializers.CharField(
        source="name",
        read_only=True,
    )

    image = serializers.ImageField(read_only=True)

    quantity = serializers.SerializerMethodField()
    damaged_quantity = serializers.SerializerMethodField()
    total_quantity = serializers.SerializerMethodField()
    updated_at = serializers.SerializerMethodField()

    stock_status = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "product_id",
            "stock_no",
            "product_name",
            "brand",
            "model",
            "category",
            "group",
            "unit",
            "purchase_price",
            "sale_price",
            "mrp",
            "image",
            "stock_status",
            "quantity",
            "damaged_quantity",
            "total_quantity",
            "updated_at",
        ]

    def get_balance(self, obj):
        balances = getattr(
            obj,
            "active_business_balances",
            [],
        )
        return balances[0] if balances else None

    def get_quantity(self, obj):
        balance = self.get_balance(obj)
        return str(balance.quantity) if balance else "0.000"

    def get_damaged_quantity(self, obj):
        balance = self.get_balance(obj)
        return (
            str(balance.damaged_quantity)
            if balance
            else "0.000"
        )

    def get_total_quantity(self, obj):
        balance = self.get_balance(obj)

        return (
            str(balance.quantity + balance.damaged_quantity)
            if balance
            else "0.000"
        )

    def get_updated_at(self, obj):
        balance = self.get_balance(obj)
        return balance.updated_at if balance else None

    def get_stock_status(self, obj):
        balance = getattr(obj, "active_business_balances", [])
        balance = balance[0] if balance else None

        if not balance:
            return "out_of_stock"

        quantity = balance.quantity

        if quantity <= 0:
            return "out_of_stock"

        return "in_stock"


# ==================================================
# STOCK MOVEMENT RESPONSE
# ==================================================

class StockMovementSerializer(serializers.ModelSerializer):
    product_id = serializers.IntegerField(
        source="stock_balance.product_id",
        read_only=True,
    )

    stock_no = serializers.CharField(
        source="stock_balance.product.stock_no",
        read_only=True,
    )

    product_name = serializers.CharField(
        source="stock_balance.product.name",
        read_only=True,
    )

    business_id = serializers.IntegerField(
        source="stock_balance.business_id",
        read_only=True,
    )

    created_by_name = serializers.CharField(
        source="created_by.get_full_name",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = StockMovement
        fields = [
            "id",
            "product_id",
            "stock_no",
            "product_name",
            "business_id",
            "stock_condition",
            "movement_type",
            "quantity",
            "balance_before",
            "balance_after",
            "reference",
            "notes",
            "created_by",
            "created_by_name",
            "created_at",
        ]
        read_only_fields = fields


# ==================================================
# STOCK QUANTITY INPUT
# ==================================================

class StockQuantityField(serializers.Field):
    """
    Accept only decimal strings with maximum 3 decimal places.
    Reject integers, floats and booleans.
    """

    def to_internal_value(self, data):
        if not isinstance(data, str):
            raise serializers.ValidationError(
                "Quantity must be sent as a decimal string."
            )

        try:
            return validate_quantity(data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages)

    def to_representation(self, value):
        return str(value)


# ==================================================
# STOCK MOVEMENT CREATE
# ==================================================


# ==================================================
# STOCK MOVEMENT CREATE
# ==================================================

class StockMovementCreateSerializer(serializers.Serializer):

    ALLOWED_MOVEMENT_TYPES = [
        StockMovement.MovementType.OPENING,
        StockMovement.MovementType.PURCHASE_RECEIPT,
        StockMovement.MovementType.SALE_ISSUE,
        StockMovement.MovementType.SALE_RETURN,
        StockMovement.MovementType.PURCHASE_RETURN,
        StockMovement.MovementType.ADJUSTMENT_IN,
        StockMovement.MovementType.ADJUSTMENT_OUT,
    ]

    ADJUSTMENT_ACTIONS = [
        "increase",
        "decrease",
        "remove_damaged",
    ]

    ADJUSTMENT_REASONS = {
        "increase": [
            "received",
            "found",
            "count_correction",
            "other",
        ],
        "decrease": [
            "damaged",
            "lost",
            "expired",
            "used",
            "count_correction",
            "other",
        ],
        "remove_damaged": [
            "disposed",
            "written_off",
            "other",
        ],
    }

    product_id = serializers.IntegerField(min_value=1)

    movement_type = serializers.ChoiceField(
        choices=ALLOWED_MOVEMENT_TYPES,
    )

    stock_condition = serializers.ChoiceField(
        choices=StockMovement.StockCondition.choices,
        default=StockMovement.StockCondition.GOOD,
    )

    quantity = StockQuantityField()

    reference = serializers.CharField(
        max_length=100,
        required=False,
        allow_blank=True,
        default="",
    )

    notes = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
    )

    # Used only for manual adjustment requests.
    adjustment_action = serializers.ChoiceField(
        choices=ADJUSTMENT_ACTIONS,
        required=False,
    )

    adjustment_reason = serializers.CharField(
        required=False,
        allow_blank=False,
    )

    def validate_product_id(self, value):
        business = self.context["request"].active_business

        product = get_product_for_business(
            business=business,
            product_id=value,
        )

        if not product:
            raise serializers.ValidationError(
                "Product not found in your active business catalog."
            )

        return product

    def validate(self, attrs):
        attrs["product"] = attrs.pop("product_id")

        movement_type = attrs["movement_type"]
        adjustment_action = attrs.get("adjustment_action")
        adjustment_reason = attrs.get("adjustment_reason")

        adjustment_types = {
            StockMovement.MovementType.ADJUSTMENT_IN,
            StockMovement.MovementType.ADJUSTMENT_OUT,
        }

        # --------------------------------------------------
        # MANUAL ADJUSTMENT VALIDATION
        # --------------------------------------------------

        if movement_type in adjustment_types:

            if not adjustment_action:
                raise serializers.ValidationError({
                    "adjustment_action": (
                        "This field is required for stock adjustments."
                    )
                })

            if not adjustment_reason:
                raise serializers.ValidationError({
                    "adjustment_reason": (
                        "Please select an adjustment reason."
                    )
                })

            allowed_reasons = self.ADJUSTMENT_REASONS[
                adjustment_action
            ]

            if adjustment_reason not in allowed_reasons:
                raise serializers.ValidationError({
                    "adjustment_reason": (
                        "Invalid reason for this adjustment action."
                    )
                })

            expected_movement_type = (
                StockMovement.MovementType.ADJUSTMENT_IN
                if adjustment_action == "increase"
                else StockMovement.MovementType.ADJUSTMENT_OUT
            )

            if movement_type != expected_movement_type:
                raise serializers.ValidationError({
                    "movement_type": (
                        "Movement type does not match adjustment action."
                    )
                })

        # --------------------------------------------------
        # NON-ADJUSTMENT MOVEMENTS
        # --------------------------------------------------

        else:

            if adjustment_action or adjustment_reason:
                raise serializers.ValidationError({
                    "adjustment_action": (
                        "Adjustment fields are only allowed "
                        "for adjustment movements."
                    )
                })

        return attrs
