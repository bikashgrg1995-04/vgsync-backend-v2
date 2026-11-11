from django.conf import settings
from django.db import models


class Task(models.Model):
    """
    A piece of work handed to a team member. The person it is assigned to
    moves it along (to do → in progress → done) and the history of every
    step is kept in ``TaskEvent`` so it can be tracked.
    """

    class Status(models.TextChoices):
        TODO = "todo", "To do"
        IN_PROGRESS = "in_progress", "In progress"
        DONE = "done", "Done"
        CANCELLED = "cancelled", "Cancelled"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        NORMAL = "normal", "Normal"
        HIGH = "high", "High"

    business = models.ForeignKey(
        "businesses.Business", on_delete=models.PROTECT, related_name="tasks"
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tasks"
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="tasks_assigned",
    )
    priority = models.CharField(
        max_length=10, choices=Priority.choices, default=Priority.NORMAL
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.TODO
    )
    due_date = models.DateField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["business", "status"]),
            models.Index(fields=["assigned_to", "status"]),
        ]

    def __str__(self):
        return self.title

    @property
    def is_open(self):
        return self.status in (self.Status.TODO, self.Status.IN_PROGRESS)


class TaskEvent(models.Model):
    """One step in a task's life: created, handed over, moved, commented."""

    class Kind(models.TextChoices):
        CREATED = "created", "Created"
        EDITED = "edited", "Edited"
        REASSIGNED = "reassigned", "Reassigned"
        STATUS = "status", "Status changed"
        COMMENT = "comment", "Comment"

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )
    actor_name = models.CharField(max_length=150, blank=True)
    from_status = models.CharField(max_length=12, blank=True)
    to_status = models.CharField(max_length=12, blank=True)
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
