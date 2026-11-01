from django.contrib import admin

from .models import PayrollRecord, SalaryProfile


@admin.register(SalaryProfile)
class SalaryProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "pay_type", "rate", "allowances", "updated_at")
    search_fields = ("user__username", "user__first_name", "user__last_name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(PayrollRecord)
class PayrollRecordAdmin(admin.ModelAdmin):
    """Read-only: payroll is changed through the API so every change is validated and attributed."""

    list_display = (
        "staff",
        "business",
        "period_start",
        "period_end",
        "net_salary",
        "status",
        "paid_on",
    )
    list_filter = ("status", "business")
    search_fields = ("staff__username", "staff__first_name", "staff__last_name")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
