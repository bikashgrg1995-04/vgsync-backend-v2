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
        FOLLOWUP_DUE = "followup_due", "Follow-up due"
        FOLLOWUP_OVERDUE = "followup_overdue", "Follow-up overdue"

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="notifications",
    )
    followup = models.ForeignKey(
        "sales.Followup",
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    title = models.CharField(max_length=200)
    body = models.CharField(max_length=300, blank=True)
    due_date = models.DateField()
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
