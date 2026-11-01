
from django.contrib import admin

from .models import LocalProduct, Product, StockBalance, StockMovement


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "stock_no",
        "name",
        "owner_business",
        "category",
        "brand",
        "unit",
        "is_active",
    )
    list_filter = (
        "owner_business",
        "category",
        "is_active",
    )
    search_fields = (
        "stock_no",
        "name",
        "brand",
        "model",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
    )
    ordering = ("name",)

    def save_model(self, request, obj, form, change):
        obj.full_clean()
        super().save_model(request, obj, form, change)


@admin.register(LocalProduct)
class LocalProductAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "business",
        "brand",
        "unit",
        "purchase_price",
        "sale_price",
        "is_active",
    )
    list_filter = (
        "business",
        "is_active",
    )
    search_fields = (
        "name",
        "brand",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
    )
    ordering = ("name",)

    def save_model(self, request, obj, form, change):
        obj.full_clean()
        super().save_model(request, obj, form, change)


@admin.register(StockBalance)
class StockBalanceAdmin(admin.ModelAdmin):
    list_display = (
        "product",
        "business",
        "quantity",
        "updated_at",
    )
    list_filter = (
        "business",
    )
    search_fields = (
        "product__stock_no",
        "product__name",
        "business__name",
    )
    readonly_fields = (
        "product",
        "business",
        "quantity",
        "updated_at",
    )
    ordering = ("product__name",)

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm("inventory.view_stockbalance")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = (
        "stock_balance",
        "movement_type",
        "quantity",
        "balance_before",
        "balance_after",
        "created_by",
        "created_at",
    )
    list_filter = (
        "movement_type",
        "created_at",
    )
    search_fields = (
        "stock_balance__product__stock_no",
        "stock_balance__product__name",
        "reference",
    )
    readonly_fields = (
        "stock_balance",
        "movement_type",
        "quantity",
        "balance_before",
        "balance_after",
        "reference",
        "notes",
        "created_by",
        "created_at",
    )
    ordering = ("-created_at",)

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm("inventory.view_stockmovement")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
