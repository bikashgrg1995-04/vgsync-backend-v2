from django.conf import settings
from django.db import models


class Faq(models.Model):
    """
    A frequently asked question shown on the Help & Support page. FAQs are
    the same for every business; the VGSync team manages them in Django Admin.
    """

    question = models.CharField(max_length=200)
    answer = models.TextField()
    sort_order = models.PositiveIntegerField(
        default=0, help_text="Lower numbers are shown first."
    )
    is_published = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "FAQ"
        verbose_name_plural = "FAQs"

    def __str__(self):
        return self.question


class SupportTicket(models.Model):
    """
    A request for help from someone in a business. The VGSync team follows
    it up and moves the status along in Django Admin.
    """

    class Category(models.TextChoices):
        INVENTORY = "inventory", "Inventory"
        PURCHASE = "purchase", "Purchase"
        SALES = "sales", "Sales"
        PAYMENTS = "payments", "Payments"
        REPORTS = "reports", "Reports"
        ACCOUNT = "account", "Account & branches"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In Progress"
        RESOLVED = "resolved", "Resolved"

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="support_tickets",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="support_tickets",
    )

    subject = models.CharField(max_length=150)
    category = models.CharField(
        max_length=20, choices=Category.choices, default=Category.OTHER
    )
    description = models.TextField()
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["business", "-id"])]

    @property
    def reference(self):
        """The number people quote, like ``TKT-00124``."""
        return f"TKT-{self.pk:05d}"

    def __str__(self):
        return f"{self.reference} {self.subject}"
