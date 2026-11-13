"""
What happens to a task: each step is written to its history and the right
people are told. Nothing here checks who is allowed; the view does.
"""

import calendar
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.notifications.models import Notification
from apps.notifications.services import notify

from .models import Task, TaskEvent


def _name(user):
    return (user.get_full_name() or user.username) if user else ""


def _link(task):
    return f"/dashboard/tasks?id={task.pk}"


def _event(task, actor, kind, **extra):
    return TaskEvent.objects.create(
        task=task, kind=kind, actor=actor, actor_name=_name(actor), **extra
    )


def _tell(task, recipient, actor, kind, title, body=""):
    """Tell one person about a change, unless they made it themselves."""
    if recipient is None or recipient.pk == getattr(actor, "pk", None):
        return

    notify(task.business, recipient, kind, title, body, task=task, link=_link(task))


def next_due(due, repeat, today):
    """The next date in the cycle after ``due``, never in the past."""
    def step(day):
        if repeat == Task.Repeat.DAILY:
            return day + timedelta(days=1)

        if repeat == Task.Repeat.WEEKLY:
            return day + timedelta(days=7)

        month = day.month % 12 + 1
        year = day.year + (day.month == 12)

        return day.replace(
            year=year, month=month,
            day=min(day.day, calendar.monthrange(year, month)[1]),
        )

    following = step(due)

    while following < today:
        following = step(following)

    return following


def _repeat_if_due(task, actor):
    """Finishing a repeating task makes the next one, once."""
    if task.repeat == Task.Repeat.NONE or task.repeat_made or not task.due_date:
        return

    task.repeat_made = True
    task.save(update_fields=["repeat_made"])
    following = create_task(
        task.business,
        task.assigned_by,
        task.assigned_to,
        title=task.title,
        description=task.description,
        priority=task.priority,
        repeat=task.repeat,
        due_date=next_due(task.due_date, task.repeat, timezone.localdate()),
    )
    _event(task, actor, TaskEvent.Kind.COMMENT,
           note=f"Repeats: next task due {following.due_date:%Y-%m-%d}")


@transaction.atomic
def create_task(business, actor, assignee, **fields):
    task = Task.objects.create(
        business=business, assigned_by=actor, assigned_to=assignee, **fields
    )
    _event(task, actor, TaskEvent.Kind.CREATED, note=f"Assigned to {_name(assignee)}")
    due = f", due {task.due_date:%Y-%m-%d}" if task.due_date else ""
    _tell(
        task,
        assignee,
        actor,
        Notification.Kind.TASK_ASSIGNED,
        f"New task: {task.title}",
        f"From {_name(actor)}{due}",
    )

    return task


@transaction.atomic
def edit_task(task, actor, changes):
    """Apply edits (title, description, priority, due date, assignee)."""
    old_assignee = task.assigned_to
    new_assignee = changes.pop("assigned_to", None)
    changed = []

    for field, value in changes.items():
        if getattr(task, field) != value:
            setattr(task, field, value)
            changed.append(field.replace("_", " "))

    if new_assignee is not None and new_assignee.pk != old_assignee.pk:
        task.assigned_to = new_assignee

    task.save()

    if changed:
        _event(task, actor, TaskEvent.Kind.EDITED, note="Changed " + ", ".join(changed))

    if new_assignee is not None and new_assignee.pk != old_assignee.pk:
        _event(
            task,
            actor,
            TaskEvent.Kind.REASSIGNED,
            note=f"{_name(old_assignee)} → {_name(new_assignee)}",
        )
        _tell(
            task,
            new_assignee,
            actor,
            Notification.Kind.TASK_ASSIGNED,
            f"New task: {task.title}",
            f"From {_name(actor)}",
        )
        _tell(
            task,
            old_assignee,
            actor,
            Notification.Kind.TASK_UPDATED,
            f"Task moved to {_name(new_assignee)}: {task.title}",
        )
    elif changed:
        _tell(
            task,
            task.assigned_to,
            actor,
            Notification.Kind.TASK_UPDATED,
            f"Task updated: {task.title}",
            "Changed " + ", ".join(changed),
        )

    return task


@transaction.atomic
def change_status(task, actor, status, note=""):
    if status == task.status:
        return task

    before = task.status
    now = timezone.now()
    task.status = status

    if status == Task.Status.IN_PROGRESS and task.started_at is None:
        task.started_at = now

    task.completed_at = now if status == Task.Status.DONE else None
    task.save()

    _event(
        task,
        actor,
        TaskEvent.Kind.STATUS,
        from_status=before,
        to_status=status,
        note=note[:500],
    )

    label = task.get_status_display()

    if status == Task.Status.DONE:
        _repeat_if_due(task, actor)
        _tell(
            task,
            task.assigned_by,
            actor,
            Notification.Kind.TASK_DONE,
            f"Task done: {task.title}",
            f"{_name(actor)} finished it" + (f": {note}" if note else ""),
        )
    elif status == Task.Status.CANCELLED:
        _tell(
            task,
            task.assigned_to,
            actor,
            Notification.Kind.TASK_UPDATED,
            f"Task cancelled: {task.title}",
            note,
        )
    else:
        # Someone other than the assignee moved it: the assignee should know.
        _tell(
            task,
            task.assigned_to,
            actor,
            Notification.Kind.TASK_UPDATED,
            f"Task set to {label}: {task.title}",
            note,
        )

    return task


@transaction.atomic
def add_comment(task, actor, note):
    _event(task, actor, TaskEvent.Kind.COMMENT, note=note[:500])
    task.save(update_fields=["updated_at"])

    for person in {task.assigned_to, task.assigned_by}:
        _tell(
            task,
            person,
            actor,
            Notification.Kind.TASK_COMMENT,
            f"{_name(actor)} commented on: {task.title}",
            note[:300],
        )


def generate_overdue(business, today=None):
    """One heads-up, for the assignee and whoever assigned it, when a task
    passes its due date still open. Each is made once."""
    today = today or timezone.localdate()
    made = 0

    for task in Task.objects.filter(
        business=business,
        status__in=[Task.Status.TODO, Task.Status.IN_PROGRESS],
        due_date__lt=today,
    ).select_related("assigned_to", "assigned_by"):
        for person in {task.assigned_to, task.assigned_by}:
            made += notify(
                business,
                person,
                Notification.Kind.TASK_OVERDUE,
                f"Task overdue: {task.title}",
                f"Was due {task.due_date:%Y-%m-%d}",
                task=task,
                link=_link(task),
                dedupe=f"task:{task.pk}:overdue:{person.pk}",
            )

    return made


@transaction.atomic
def add_attachment(task, actor, upload):
    from .models import TaskAttachment

    item = TaskAttachment.objects.create(
        task=task,
        file=upload,
        original_name=upload.name[:255],
        size=upload.size,
        uploaded_by=actor,
    )
    _event(task, actor, TaskEvent.Kind.COMMENT, note=f"Attached {item.original_name}")
    task.save(update_fields=["updated_at"])

    for person in {task.assigned_to, task.assigned_by}:
        _tell(
            task, person, actor, Notification.Kind.TASK_UPDATED,
            f"File added to: {task.title}", item.original_name,
        )

    return item
