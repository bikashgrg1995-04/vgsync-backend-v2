"""
The pieces of the owner dashboard that look ahead or compare periods: totals
against the period before, what falls due soon, and the latest activity.
"""

import datetime
from collections import defaultdict

from django.db.models import Sum
from django.utils import timezone

from apps.finance.models import Expense, Income
from apps.purchases.models import Purchase, PurchasePayment
from apps.sales.models import Followup, Sale, SalePayment

from . import queries
from .queries import ZERO, money

RANGES = (7, 30, 90)


def parse_days(value):
    try:
        days = int(value)
    except (TypeError, ValueError):
        return 30

    return days if days in RANGES else 30


def _daily(queryset, date_field, amount_field, start, end):
    by_day = defaultdict(lambda: ZERO)

    for row in (
        queryset.filter(**{f"{date_field}__gte": start, f"{date_field}__lte": end})
        .values(date_field)
        .annotate(total=Sum(amount_field))
    ):
        by_day[row[date_field]] += row["total"] or ZERO

    return by_day


def _series(by_day, start, days):
    return [
        money(by_day.get(start + datetime.timedelta(days=i), ZERO)) for i in range(days)
    ]


def _block(by_day_now, by_day_before, start, days):
    now = sum(by_day_now.values(), ZERO)
    before = sum(by_day_before.values(), ZERO)
    change = None if before <= 0 else round(float((now - before) / before * 100), 1)

    return {
        "total": money(now),
        "previous": money(before),
        "change_pct": change,
        "series": _series(by_day_now, start, days),
    }


def period_overview(business, today, days):
    """Sales, purchases, income and expenses for the last ``days`` days, each against the days before."""
    start = today - datetime.timedelta(days=days - 1)
    prev_end = start - datetime.timedelta(days=1)
    prev_start = prev_end - datetime.timedelta(days=days - 1)

    sources = {
        "sales": (
            Sale.objects.filter(business=business, status=Sale.Status.CONFIRMED),
            "sale_date", "net_total",
        ),
        "purchases": (
            Purchase.objects.filter(business=business, status__in=Purchase.BILLED),
            "purchase_date", "net_total",
        ),
        "income": (
            Income.objects.filter(business=business, status="active"),
            "income_date", "amount",
        ),
        "expenses": (
            Expense.objects.filter(business=business, status="active"),
            "expense_date", "amount",
        ),
    }

    data = {
        name: _block(
            _daily(qs, date_f, amount_f, start, today),
            _daily(qs, date_f, amount_f, prev_start, prev_end),
            start, days,
        )
        for name, (qs, date_f, amount_f) in sources.items()
    }

    return {
        "days": days,
        "date_from": start,
        "date_to": today,
        "labels": [start + datetime.timedelta(days=i) for i in range(days)],
        **data,
    }


def _days_to(date, today):
    return (date - today).days


def due_soon(business, today):
    """What needs attention in the next few days (overdue items count as due now)."""
    soon3, soon7 = today + datetime.timedelta(days=3), today + datetime.timedelta(days=7)
    pending = Followup.objects.filter(business=business, status=Followup.Status.PENDING)

    emi = pending.filter(kind=Followup.Kind.EMI_DUE, due_date__lte=soon3)
    credit = pending.filter(kind=Followup.Kind.CREDIT_DUE, due_date__lte=soon7)
    service = pending.filter(
        kind__in=(Followup.Kind.MAINTENANCE_POST, Followup.Kind.MAINTENANCE_DUE)
    )

    first_emi = emi.order_by("due_date").first()
    first_credit = credit.order_by("due_date").first()

    # Purchases the business still has to pay, with a due date in the next week.
    purchases = list(
        Purchase.objects.filter(
            business=business, status__in=Purchase.BILLED,
            due_date__isnull=False, due_date__lte=soon7,
        )
    )
    paid = queries._grouped(
        PurchasePayment.objects.filter(
            purchase__in=[p.pk for p in purchases], status=PurchasePayment.Status.ACTIVE
        ),
        "purchase",
    )
    owing = [
        (p, p.net_total - paid.get(p.pk, ZERO)) for p in purchases
    ]
    owing = [(p, amount) for p, amount in owing if amount > 0]
    first_owing = min((p.due_date for p, _ in owing), default=None)

    return {
        "installments": {
            "count": emi.count(),
            "days": None if not first_emi else _days_to(first_emi.due_date, today),
        },
        "purchase_credit": {
            "amount": money(sum((a for _, a in owing), ZERO)),
            "days": None if first_owing is None else _days_to(first_owing, today),
        },
        "sales_credit": {
            "amount": money(credit.aggregate(t=Sum("amount"))["t"] or ZERO),
            "days": None if not first_credit else _days_to(first_credit.due_date, today),
        },
        "servicing": {"count": service.count()},
    }


def installments_list(business, today, limit=4):
    rows = (
        Followup.objects.filter(
            business=business, status=Followup.Status.PENDING, kind=Followup.Kind.EMI_DUE
        )
        .select_related("sale", "customer", "installment")
        .order_by("due_date")[:limit]
    )
    result = []

    for f in rows:
        total = f.sale.installments.count()
        number = f.installment.number if f.installment_id else 0
        result.append({
            "sale": f.sale.invoice_no or f.sale.sale_no,
            "customer": f.customer.name if f.customer_id else "",
            "number": number,
            "of": total,
            "due_date": f.due_date,
            "days": _days_to(f.due_date, today),
            "amount": money(f.amount or ZERO),
        })

    return result


def servicing_list(business, today, limit=4):
    rows = (
        Followup.objects.filter(
            business=business, status=Followup.Status.PENDING,
            kind__in=(Followup.Kind.MAINTENANCE_POST, Followup.Kind.MAINTENANCE_DUE),
        )
        .select_related("sale", "customer")
        .order_by("due_date")[:limit]
    )

    return [
        {
            "id": f.pk,
            "customer": f.customer.name if f.customer_id else "",
            "phone": f.customer.phone if f.customer_id else "",
            "kind": f.kind,
            "due_date": f.due_date,
            "overdue": f.due_date < today,
        }
        for f in rows
    ]


def recent_activity(business, limit=6):
    """The latest sales, payments received and purchases, newest first."""
    items = []

    for s in Sale.objects.filter(business=business, status=Sale.Status.CONFIRMED).order_by("-created_at")[:limit]:
        items.append((s.created_at, "sale", f"New Sale {s.invoice_no or s.sale_no}"))

    for p in SalePayment.objects.filter(sale__business=business, status=SalePayment.Status.ACTIVE).order_by("-paid_on", "-id")[:limit]:
        when = timezone.make_aware(datetime.datetime.combine(p.paid_on, datetime.time(12, 0)))
        items.append((when, "payment", f"Payment received (Rs. {p.amount:,.0f})"))

    for p in Purchase.objects.filter(business=business, status__in=Purchase.BILLED).order_by("-created_at")[:limit]:
        items.append((p.created_at, "purchase", f"New Purchase PUR-{p.number:06d}"))

    items.sort(key=lambda row: row[0], reverse=True)

    return [{"at": at, "kind": kind, "text": text} for at, kind, text in items[:limit]]
