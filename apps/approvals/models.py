from django.conf import settings
from django.db import models


class Kind(models.TextChoices):
    PURCHASE = "purchase", "High-value purchase"
    EXPENSE = "expense", "High-value expense"
    STOCK_ADJUSTMENT = "stock_adjustment", "Large stock adjustment"
    STOCK_WRITE_OFF = "stock_write_off", "Stock write-off"


# The permission that lets a person decide each kind of request.
APPROVER_PERMISSION = {
    Kind.PURCHASE: "purchases.approve",
    Kind.EXPENSE: "finance.approve",
    Kind.STOCK_ADJUSTMENT: "inventory.approve",
    Kind.STOCK_WRITE_OFF: "inventory.approve",
}


class ApprovalRule(models.Model):
    """
    When an operation needs approval in one business. Set by the owner;
    no amount is built in. No rule (or an inactive one) means no approval.
    """

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.CASCADE,
        related_name="approval_rules",
    )
    kind = models.CharField(max_length=30, choices=Kind.choices)
    # Operations worth this much or more need approval. Zero means always.
    threshold = models.DecimalField(max_digits=14, decimal_places=2)
    is_active = models.BooleanField(default=True)
    allow_self_approval = models.BooleanField(default=False)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["kind"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "kind"],
                name="unique_approval_rule_per_kind",
            ),
            models.CheckConstraint(
                condition=models.Q(threshold__gte=0),
                name="approval_threshold_not_negative",
            ),
        ]

    def __str__(self):
        return f"{self.kind} >= {self.threshold}"


class ApprovalRequest(models.Model):
    """
    A held operation. The client repeats the same request with
    ``approval_id`` once it is approved; the backend checks that the request
    is approved, unused, from the same person and the same payload.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.CASCADE,
        related_name="approval_requests",
    )
    kind = models.CharField(max_length=30, choices=Kind.choices)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    summary = models.CharField(max_length=200, blank=True)
    # What was asked for, and its fingerprint (see services.fingerprint).
    payload = models.JSONField(default=dict)
    payload_hash = models.CharField(max_length=64)
    # The record the approval ended up creating, once it is used.
    reference = models.CharField(max_length=100, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="approval_requests",
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    rejection_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["business", "status", "-id"])]

    def __str__(self):
        return f"{self.kind} {self.amount} ({self.status})"
