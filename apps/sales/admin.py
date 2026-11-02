from django.contrib import admin

from .models import BikeUnit, Customer, EmiInstallment, Followup, Sale, SaleItem, SalePayment


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "phone", "pan_number", "is_active")
    list_filter = ("is_active", "business")
    search_fields = ("name", "phone", "pan_number")


class ReadOnlyAdmin(admin.ModelAdmin):
    """Sales change stock and money, so they go through the API."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Sale)
class SaleAdmin(ReadOnlyAdmin):
    list_display = ("sale_no", "business", "customer", "status", "net_total", "sale_date")
    list_filter = ("status", "business")
    search_fields = ("customer__name", "walk_in_name")
    inlines = [SaleItemInline]


@admin.register(SalePayment)
class SalePaymentAdmin(ReadOnlyAdmin):
    list_display = ("sale", "amount", "method", "paid_on", "status")
    list_filter = ("status", "method")


@admin.register(BikeUnit)
class BikeUnitAdmin(ReadOnlyAdmin):
    list_display = ("chassis_no", "engine_no", "product", "business", "status")
    list_filter = ("status", "business")
    search_fields = ("chassis_no", "engine_no")


@admin.register(Followup)
class FollowupAdmin(ReadOnlyAdmin):
    list_display = ("kind", "sale", "due_date", "status")
    list_filter = ("kind", "status")
