from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """
    Who changed what, and when. Rows are written by signals for the tracked
    models (see ``signals.TRACKED``) and are never edited or deleted.
    """

    class Action(models.TextChoices):
        CREATE = "create", "Create"
        UPDATE = "update", "Update"
        DELETE = "delete", "Delete"

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # Kept as text so the log still reads well if the user is removed.
    actor_name = models.CharField(max_length=150, blank=True)

    action = models.CharField(max_length=10, choices=Action.choices)
    # ``app_label.model``, for example ``sales.sale``.
    model = models.CharField(max_length=100)
    object_id = models.CharField(max_length=40)
    object_repr = models.CharField(max_length=200, blank=True)

    # create/delete: the values. update: ``{field: [old, new]}``.
    changes = models.JSONField(default=dict)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["business", "-id"]),
            models.Index(fields=["business", "model", "object_id"]),
        ]

    def __str__(self):
        return f"{self.action} {self.model} {self.object_id}"
