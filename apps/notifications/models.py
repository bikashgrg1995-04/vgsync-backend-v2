from django.conf import settings
from django.db import models


class Notification(models.Model):
    """
    Something the team should act on. Notifications belong to a business and
    are shown to everyone who may view reminders there; each person marks
    them read for themselves. They are made from follow-ups that are due or
    overdue (see ``services.generate_for_business``).
    """

    class Kind(models.TextChoices):
        FOLLOWUP_SOON = "followup_soon", "Follow-up coming up"
        FOLLOWUP_DUE = "followup_due", "Follow-up due"
        FOLLOWUP_OVERDUE = "followup_overdue", "Follow-up overdue"
        PAYABLE_SOON = "payable_soon", "Supplier payment coming up"
        PAYABLE_DUE = "payable_due", "Supplier payment due"
        PAYABLE_OVERDUE = "payable_overdue", "Supplier payment overdue"
        TASK_ASSIGNED = "task_assigned", "Task assigned"
        TASK_UPDATED = "task_updated", "Task updated"
        TASK_DONE = "task_done", "Task done"
        TASK_COMMENT = "task_comment", "Task comment"
        TASK_OVERDUE = "task_overdue", "Task overdue"

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="notifications",
    )
    followup = models.ForeignKey(
        "sales.Followup",
        on_delete=models.CASCADE,
        related_name="notifications",
        null=True,
        blank=True,
    )
    purchase = models.ForeignKey(
        "purchases.Purchase",
        on_delete=models.CASCADE,
        related_name="notifications",
        null=True,
        blank=True,
    )
    task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.CASCADE,
        related_name="notifications",
        null=True,
        blank=True,
    )
    # Empty = for everyone who may see reminders; set = for that person only.
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="+",
        null=True,
        blank=True,
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    title = models.CharField(max_length=200)
    body = models.CharField(max_length=300, blank=True)
    due_date = models.DateField(null=True, blank=True)
    # Where clicking it should go, for example a task.
    link = models.CharField(max_length=200, blank=True)
    # Stops the same notification being made twice.
    dedupe_key = models.CharField(max_length=60)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["business", "-id"])]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "dedupe_key"],
                name="unique_notification_per_business",
            ),
        ]

    def __str__(self):
        return self.title


class NotificationRead(models.Model):
    """One person has read one notification."""

    notification = models.ForeignKey(
        Notification, on_delete=models.CASCADE, related_name="reads"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["notification", "user"], name="unique_read_per_user"
            ),
        ]
