from rest_framework import serializers

from .models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    summary = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = (
            "id",
            "created_at",
            "actor",
            "actor_name",
            "action",
            "model",
            "object_id",
            "object_repr",
            "summary",
            "changes",
        )
        read_only_fields = fields

    def get_summary(self, obj):
        """One line: ``status: active -> void; amount: 100.00 -> 150.00``."""
        if obj.action != AuditLog.Action.UPDATE:
            return f"{obj.get_action_display()} {obj.object_repr}"

        return "; ".join(
            f"{name}: {old} -> {new}" for name, (old, new) in obj.changes.items()
        )
