from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.businesses.models import Business
from apps.sales.models import Followup

from .models import Notification

# How long a business is left alone after its notifications were made.
GENERATE_EVERY = 15 * 60

# How many days ahead a follow-up or credit due date starts to be flagged.
WARN_DAYS_AHEAD = 3


def _text(followup, today):
    who = followup.customer.name if followup.customer_id else "Walk-in customer"
    label = followup.get_kind_display()
    days = (followup.due_date - today).days

    if days > 0:
        when = f"due in {days} day{'s' if days != 1 else ''} ({followup.due_date:%Y-%m-%d})"
    elif days == 0:
        when = f"due today ({followup.due_date:%Y-%m-%d})"
    else:
        when = f"overdue since {followup.due_date:%Y-%m-%d}"

    title = f"{label} {when}: {who}"

    parts = [f"Sale {followup.sale.sale_no}"]

    if followup.amount:
        parts.append(f"amount {followup.amount:.2f}")

    if followup.note:
        parts.append(followup.note)

    return title[:200], ", ".join(parts)[:300]


def generate_payables(business, today=None):
    """
    The same three heads-ups for money owed to a supplier: three days before
    the payment is due, on the day, and when it is late. Only purchases that
    arrived and are not yet paid up.
    """
    from apps.purchases.models import Purchase
    from apps.purchases.services import credit_applied_total, paid_total

    today = today or timezone.localdate()
    made = 0

    purchases = Purchase.objects.filter(
        business=business,
        status__in=[Purchase.Status.PARTIAL, Purchase.Status.RECEIVED],
        due_date__isnull=False,
        due_date__lte=today + timedelta(days=WARN_DAYS_AHEAD),
    ).select_related("supplier")

    for purchase in purchases:
        owed = purchase.net_total - paid_total(purchase) - credit_applied_total(purchase)

        if owed <= 0:
            continue

        days = (purchase.due_date - today).days

        if days > 0:
            state, kind = "soon", Notification.Kind.PAYABLE_SOON
            when = f"due in {days} day{'s' if days != 1 else ''} ({purchase.due_date:%Y-%m-%d})"
        elif days == 0:
            state, kind = "due", Notification.Kind.PAYABLE_DUE
            when = f"due today ({purchase.due_date:%Y-%m-%d})"
        else:
            state, kind = "overdue", Notification.Kind.PAYABLE_OVERDUE
            when = f"overdue since {purchase.due_date:%Y-%m-%d}"

        _, created = Notification.objects.get_or_create(
            business=business,
            dedupe_key=f"purchase:{purchase.pk}:{state}",
            defaults={
                "purchase": purchase,
                "kind": kind,
                "title": f"Supplier payment {when}: {purchase.supplier.name}"[:200],
                "body": f"{purchase.purchase_no}, {owed:.2f} left to pay"[:300],
                "due_date": purchase.due_date,
                "link": f"/dashboard/purchase/detail?id={purchase.pk}",
            },
        )
        made += created

    return made


@transaction.atomic
def generate_for_business(business, today=None):
    """
    Make a notification for every pending follow-up (a call, an instalment
    or a credit to collect) three days before it is due, again on the day,
    and again when it becomes overdue. Safe to run again and again: each one
    is made once. Returns how many were made.
    """
    today = today or timezone.localdate()

    followups = Followup.objects.filter(
        business=business,
        status=Followup.Status.PENDING,
        due_date__lte=today + timedelta(days=WARN_DAYS_AHEAD),
    ).select_related("customer", "sale")

    made = 0

    for followup in followups:
        if followup.due_date > today:
            state, kind = "soon", Notification.Kind.FOLLOWUP_SOON
        elif followup.due_date == today:
            state, kind = "due", Notification.Kind.FOLLOWUP_DUE
        else:
            state, kind = "overdue", Notification.Kind.FOLLOWUP_OVERDUE

        title, body = _text(followup, today)

        _, created = Notification.objects.get_or_create(
            business=business,
            dedupe_key=f"followup:{followup.pk}:{state}",
            defaults={
                "followup": followup,
                "kind": kind,
                "title": title,
                "body": body,
                "due_date": followup.due_date,
                # An instalment opens its sale, where the schedule is.
                "link": (
                    f"/dashboard/sales/detail?id={followup.sale_id}"
                    if followup.kind
                    in (
                        Followup.Kind.EMI_DUE,
                        Followup.Kind.FINANCER_DUE,
                        Followup.Kind.FREE_SERVICE_DUE,
                    )
                    and followup.sale_id
                    else ""
                ),
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
        generate_payables(business, today)
        generate_overdue(business, today)


def generate_everywhere(today=None):
    """For a daily job: every active business."""
    total = 0

    for business in Business.objects.filter(is_active=True):
        from apps.tasks.services import generate_overdue

        total += generate_for_business(business, today)
        total += generate_payables(business, today)
        total += generate_overdue(business, today)

    return total
