from django.contrib import admin

from .models import Purchase, PurchaseItem, PurchasePayment, Supplier


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "phone", "pan_number", "is_active")
    list_filter = ("is_active", "business")
    search_fields = ("name", "phone", "pan_number")


class ReadOnlyAdmin(admin.ModelAdmin):
    """Purchases change stock and money, so they go through the API."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class PurchaseItemInline(admin.TabularInline):
    model = PurchaseItem
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Purchase)
class PurchaseAdmin(ReadOnlyAdmin):
    list_display = ("purchase_no", "business", "supplier", "purchase_date", "net_total", "status")
    list_filter = ("status", "business")
    search_fields = ("bill_no", "supplier__name")
    inlines = [PurchaseItemInline]


@admin.register(PurchasePayment)
class PurchasePaymentAdmin(ReadOnlyAdmin):
    list_display = ("purchase", "amount", "paid_on", "method", "status")
    list_filter = ("method", "status")
