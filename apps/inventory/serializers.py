
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
            "reorder_level",
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
        extra_kwargs = {
            "mrp": {"min_value": 0},
            "purchase_price": {"min_value": 0},
            "sale_price": {"min_value": 0},
            "reorder_level": {"min_value": 0},
        }

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

    quantity = serializers.SerializerMethodField()
    damaged_quantity = serializers.SerializerMethodField()
    total_quantity = serializers.SerializerMethodField()
    stock_status = serializers.SerializerMethodField()
    updated_at = serializers.SerializerMethodField()

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
            "reorder_level",
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

    def get_stock_status(self, obj):
        """out (nothing good on hand), low (at/below reorder level) or in_stock."""
        balance = self.get_balance(obj)
        good = balance.quantity if balance else 0

        if good <= 0:
            return "out"
        if obj.reorder_level > 0 and good <= obj.reorder_level:
            return "low"
        return "in_stock"

    def get_updated_at(self, obj):
        balance = self.get_balance(obj)
        return balance.updated_at if balance else None


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
        return attrs
