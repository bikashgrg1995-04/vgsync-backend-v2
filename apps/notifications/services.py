from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.businesses.models import Business
from apps.sales.models import Followup

from .models import Notification

# How long a business is left alone after its notifications were made.
GENERATE_EVERY = 15 * 60


def _text(followup, overdue):
    who = followup.customer.name if followup.customer_id else "Walk-in customer"
    label = followup.get_kind_display()
    when = "overdue since" if overdue else "due"
    title = f"{label} {when} {followup.due_date:%Y-%m-%d}: {who}"

    parts = [f"Sale {followup.sale.sale_no}"]

    if followup.amount:
        parts.append(f"amount {followup.amount:.2f}")

    if followup.note:
        parts.append(followup.note)

    return title[:200], ", ".join(parts)[:300]


@transaction.atomic
def generate_for_business(business, today=None):
    """
    Make a notification for every pending follow-up that is due today, and
    another when it becomes overdue. Safe to run again and again: each one
    is made once. Returns how many were made.
    """
    today = today or timezone.localdate()

    followups = Followup.objects.filter(
        business=business,
        status=Followup.Status.PENDING,
        due_date__lte=today,
    ).select_related("customer", "sale")

    made = 0

    for followup in followups:
        overdue = followup.due_date < today
        state = "overdue" if overdue else "due"
        title, body = _text(followup, overdue)

        _, created = Notification.objects.get_or_create(
            business=business,
            dedupe_key=f"followup:{followup.pk}:{state}",
            defaults={
                "followup": followup,
                "kind": (
                    Notification.Kind.FOLLOWUP_OVERDUE
                    if overdue
                    else Notification.Kind.FOLLOWUP_DUE
                ),
                "title": title,
                "body": body,
                "due_date": followup.due_date,
            },
        )
        made += created

    return made


def notify(business, recipient, kind, title, body="", *, task=None, link="", dedupe=None):
    """Tell one person something. Returns 1 if it was made, 0 if the same
    notification already exists."""
    import uuid

    _, created = Notification.objects.get_or_create(
        business=business,
        dedupe_key=dedupe or f"n:{uuid.uuid4().hex}",
        defaults={
            "recipient": recipient,
            "kind": kind,
            "title": title[:200],
            "body": body[:300],
            "task": task,
            "link": link,
        },
    )

    return int(created)


def refresh(business):
    """
    Make the business's notifications now, but at most once every few
    minutes, so listing them stays cheap and no scheduler is needed.
    """
    today = timezone.localdate()

    if cache.add(f"notifications:{business.pk}:{today}", 1, GENERATE_EVERY):
        from apps.tasks.services import generate_overdue

        generate_for_business(business, today)
        generate_overdue(business, today)


def generate_everywhere(today=None):
    """For a daily job: every active business."""
    total = 0

    for business in Business.objects.filter(is_active=True):
        from apps.tasks.services import generate_overdue

        total += generate_for_business(business, today)
        total += generate_overdue(business, today)

    return total
