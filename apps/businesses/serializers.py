
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
