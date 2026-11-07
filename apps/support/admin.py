from django.contrib import admin

from .models import Faq, SupportTicket


@admin.register(Faq)
class FaqAdmin(admin.ModelAdmin):
    list_display = ("question", "sort_order", "is_published")
    list_editable = ("sort_order", "is_published")
    search_fields = ("question", "answer")


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "subject",
        "business",
        "created_by",
        "category",
        "status",
        "created_at",
    )
    list_filter = ("status", "category")
    list_select_related = ("business", "created_by")
    search_fields = ("subject", "description", "business__name")
    readonly_fields = ("business", "created_by", "created_at", "updated_at")
