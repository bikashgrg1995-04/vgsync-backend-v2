from rest_framework import serializers

from .models import ApprovalRequest, ApprovalRule, Kind


class ApprovalRuleSerializer(serializers.ModelSerializer):
    kind = serializers.ChoiceField(choices=Kind.choices)
    threshold = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=0
    )

    class Meta:
        model = ApprovalRule
        fields = (
            "id", "kind", "threshold", "is_active",
            "allow_self_approval", "updated_at",
        )
        read_only_fields = ("id", "updated_at")


class ApprovalRequestSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(
        source="created_by.username", read_only=True
    )
    decided_by_name = serializers.CharField(
        source="decided_by.username", read_only=True, default=None
    )
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = ApprovalRequest
        fields = (
            "id", "kind", "kind_label", "status", "amount", "summary",
            "payload", "reference", "created_by", "created_by_name",
            "decided_by", "decided_by_name", "rejection_reason",
            "created_at", "decided_at", "used_at",
        )
        read_only_fields = fields
