from rest_framework import serializers

from .models import Company


class CompanySerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        fields = ["id", "name", "code", "logo", "is_active", "created_at"]
        read_only_fields = ["id", "code", "logo", "is_active", "created_at"]

    def validate_name(self, value):
        value = value.strip()

        if not value:
            raise serializers.ValidationError("This field may not be blank.")

        return value
