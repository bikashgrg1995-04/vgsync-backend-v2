from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Read only: the log is never edited."""

    list_display = ("id", "created_at", "business", "actor_name", "action", "model", "object_repr")
    list_filter = ("action", "model")
    search_fields = ("object_repr", "actor_name", "object_id")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
