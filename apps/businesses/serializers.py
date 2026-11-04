import re


from rest_framework import serializers

from .models import Business


class BranchListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Business
        fields = [
            "id",
            "name",
            "business_code",
            "business_type",
            "is_active",
        ]
        read_only_fields = fields


class BusinessSerializer(serializers.ModelSerializer):
    """
    A business of the owner's tenant. The code, type and parent are fixed
    when a branch is created; the rest can be edited.
    """

    parent_business_name = serializers.CharField(
        source="parent_business.name", read_only=True, default=None
    )

    class Meta:
        model = Business
        fields = [
            "id",
            "name",
            "business_code",
            "business_type",
            "parent_business",
            "parent_business_name",
            "address",
            "phone",
            "email",
            "is_active",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "business_type",
            "parent_business",
            "is_active",
            "created_at",
        ]

    def validate_name(self, value):
        value = value.strip()

        if not value:
            raise serializers.ValidationError("This field may not be blank.")

        return value

    def validate_business_code(self, value):
        value = value.strip().upper()

        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{1,49}", value):
            raise serializers.ValidationError(
                "Use letters, numbers, dashes or underscores (2 to 50)."
            )

        # Login matches the code without regard to case.
        if Business.objects.filter(business_code__iexact=value).exists():
            raise serializers.ValidationError("This business code is already in use.")

        return value

    def validate(self, attrs):
        if self.instance is not None and "business_code" in attrs:
            raise serializers.ValidationError(
                {"business_code": "The business code cannot be changed."}
            )

        return attrs
