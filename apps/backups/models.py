from django.conf import settings
from django.db import models


class BackupLog(models.Model):
    """
    A record that someone downloaded a backup. The backup file itself is
    built on request and handed to the owner; only this note is kept.
    """

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.CASCADE,
        related_name="backup_logs",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )
    created_by_name = models.CharField(max_length=150, blank=True)
    class Kind(models.TextChoices):
        BACKUP = "backup", "Backup"
        RESTORE = "restore", "Restore"
        SAFETY = "safety", "Safety copy before a restore"

    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.BACKUP)
    # Only safety copies keep their file, so a restore can be undone.
    data = models.BinaryField(null=True, blank=True, editable=False)
    # "business" = this business only; "company" = every branch plus the
    # Head Office.
    scope = models.CharField(max_length=10, default="business")
    business_count = models.PositiveIntegerField(default=1)
    file_count = models.PositiveIntegerField(default=0)
    row_count = models.PositiveIntegerField(default=0)
    size_bytes = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-id",)

    def __str__(self):
        return f"Backup {self.created_at:%Y-%m-%d %H:%M} by {self.created_by_name}"
