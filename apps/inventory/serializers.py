
from rest_framework import serializers

from apps.inventory.models import Product


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
