from rest_framework import serializers

from .models import BackupLog


class BackupLogSerializer(serializers.ModelSerializer):
    has_file = serializers.SerializerMethodField()

    def get_has_file(self, obj):
        return obj.kind == BackupLog.Kind.SAFETY and obj.data is not None

    class Meta:
        model = BackupLog
        fields = (
            "id",
            "kind",
            "has_file",
            "created_at",
            "created_by_name",
            "scope",
            "business_count",
            "file_count",
            "row_count",
            "size_bytes",
        )
