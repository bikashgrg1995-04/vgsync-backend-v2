from rest_framework import serializers

from .models import Faq, SupportTicket


class FaqSerializer(serializers.ModelSerializer):
    class Meta:
        model = Faq
        fields = ("id", "question", "answer")
        read_only_fields = fields


class SupportTicketSerializer(serializers.ModelSerializer):
    reference = serializers.CharField(read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    category_label = serializers.CharField(
        source="get_category_display", read_only=True
    )
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SupportTicket
        fields = (
            "id",
            "reference",
            "subject",
            "category",
            "category_label",
            "description",
            "status",
            "status_label",
            "created_by_name",
            "created_at",
            "updated_at",
        )
        # People can raise a ticket; only the VGSync team changes its status.
        read_only_fields = ("id", "status", "created_at", "updated_at")

    def get_created_by_name(self, ticket):
        user = ticket.created_by

        return user.get_full_name() or user.username

    def validate_subject(self, value):
        value = value.strip()

        if len(value) < 3:
            raise serializers.ValidationError("Please enter a short subject.")

        return value

    def validate_description(self, value):
        value = value.strip()

        if len(value) < 10:
            raise serializers.ValidationError(
                "Please describe the problem in a few words."
            )

        return value
