
from django.contrib import admin

from .models import Business


@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "business_type",
        "company",
        "parent_business",
        "business_code",
        "is_active",
    )

    list_filter = (
        "business_type",
        "company",
        "is_active",
    )

    search_fields = (
        "name",
        "business_code",
        "company__name",
    )

    readonly_fields = (
        "created_at",
    )

    autocomplete_fields = (
        "company",
        "parent_business",
        "created_by",
    )

    ordering = ("name",)
