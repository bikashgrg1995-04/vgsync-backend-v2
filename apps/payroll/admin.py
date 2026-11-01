from django.contrib import admin

from .models import PayrollEntry, PayrollPayment, SalaryProfile


@admin.register(SalaryProfile)
class SalaryProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "pay_type", "rate", "salary_day", "updated_at")
    search_fields = ("user__username", "user__first_name", "user__last_name")
    readonly_fields = ("accrued_through", "created_at", "updated_at")


class ReadOnlyAdmin(admin.ModelAdmin):
    """Pay is changed through the API so every change is validated and attributed."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PayrollEntry)
class PayrollEntryAdmin(ReadOnlyAdmin):
    list_display = ("staff", "business", "kind", "entry_date", "amount", "status")
    list_filter = ("kind", "status", "business")
    search_fields = ("staff__username", "staff__first_name", "staff__last_name")


@admin.register(PayrollPayment)
class PayrollPaymentAdmin(ReadOnlyAdmin):
    list_display = ("staff", "business", "amount", "paid_on", "method", "status")
    list_filter = ("method", "status", "business")
    search_fields = ("staff__username", "staff__first_name", "staff__last_name")
