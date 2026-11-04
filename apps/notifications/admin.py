from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "business", "kind", "due_date", "created_at")
    list_filter = ("kind",)
    search_fields = ("title", "body")
