from rest_framework import serializers

from .models import BackupLog


class BackupLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = BackupLog
        fields = (
            "id",
            "created_at",
            "created_by_name",
            "scope",
            "business_count",
            "file_count",
            "row_count",
            "size_bytes",
        )
