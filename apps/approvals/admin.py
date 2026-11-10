from django.contrib import admin

from .models import ApprovalRequest, ApprovalRule


@admin.register(ApprovalRule)
class ApprovalRuleAdmin(admin.ModelAdmin):
    list_display = ("business", "kind", "threshold", "is_active")


@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(admin.ModelAdmin):
    list_display = ("business", "kind", "amount", "status", "created_by")
    readonly_fields = [f.name for f in ApprovalRequest._meta.fields]
