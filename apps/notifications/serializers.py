from rest_framework import serializers

from .models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    is_read = serializers.BooleanField(read_only=True)
    sale = serializers.IntegerField(source="followup.sale_id", read_only=True, default=None)
    customer = serializers.IntegerField(
        source="followup.customer_id", read_only=True, default=None
    )

    class Meta:
        model = Notification
        fields = (
            "id",
            "kind",
            "title",
            "body",
            "due_date",
            "followup",
            "task",
            "link",
            "sale",
            "customer",
            "is_read",
            "created_at",
        )
        read_only_fields = fields
