"""
Payroll rules in one place: when monthly salary falls due, how payments are
matched to what is owed, and recording or voiding payments.

How "remaining" is worked out
-----------------------------
Everything the person has earned (salary, daily wages, bonuses) is listed
oldest first. Everything already given to them (payments, plus deductions,
which are money the business keeps back) forms one pool. The pool is
poured over the earnings from the oldest to the newest, so older amounts
are settled first. What is left unsettled is the remaining amount; if the
pool is bigger than all earnings, the surplus is an advance that the next
amount due will absorb. Nothing is stored, so it cannot go out of date.
"""

import calendar
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.businesses.models import Business
from apps.common.exceptions import Conflict

from rest_framework import serializers as drf

from .models import (
    IncentiveRule,
    WorkDay,
    PayrollEntry,
    PayrollPayment,
    PayType,
    SalaryProfile,
    ZERO,
)

EARNING_KINDS = (
    PayrollEntry.Kind.SALARY,
    PayrollEntry.Kind.WAGE,
    PayrollEntry.Kind.BONUS,
)


# ---------------------------------------------------------------------------
# When monthly salary falls due
# ---------------------------------------------------------------------------

def due_date(year, month, salary_day):
    """The salary day of a month; short months use their last day."""
    last = calendar.monthrange(year, month)[1]

    return date(year, month, min(salary_day, last))


def due_dates(salary_day, after, upto):
    """Every due date with ``after < date <= upto``, oldest first."""
    year, month = after.year, after.month
    dates = []

    while (year, month) <= (upto.year, upto.month):
        due = due_date(year, month, salary_day)

        if after < due <= upto:
            dates.append(due)

        month += 1

        if month == 13:
            year, month = year + 1, 1

    return dates


def payroll_business(user):
    """The business whose books hold this person's pay."""
    if user.business_id:
        return user.business

    return Business.objects.get(
        company_id=user.company_id,
        business_type=Business.BusinessType.HEAD_OFFICE,
    )


def first_accrued_through(start_date):
    return start_date - timedelta(days=1)


@transaction.atomic
def ensure_accruals(profile, today=None):
    """
    Record the monthly salary of every due date that has passed and is not
    recorded yet. Safe to call as often as needed; it only fills the gap.
    """
    today = today or timezone.localdate()

    profile = SalaryProfile.objects.select_for_update().select_related(
        "user"
    ).get(pk=profile.pk)

    if profile.pay_type != PayType.MONTHLY or not profile.user.is_active:
        return profile

    if profile.accrued_through >= today:
        return profile

    business = payroll_business(profile.user)
    amount = profile.rate + profile.allowances

    for due in due_dates(profile.salary_day, profile.accrued_through, today):
        if amount > 0:
            PayrollEntry.objects.get_or_create(
                staff=profile.user,
                kind=PayrollEntry.Kind.SALARY,
                entry_date=due,
                status=PayrollEntry.Status.ACTIVE,
                defaults={
                    "business": business,
                    "amount": amount,
                    "description": f"Salary for {due:%B %Y}",
                },
            )

    profile.accrued_through = today
    profile.save(update_fields=["accrued_through"])

    return profile


def current_cycle_start(profile, today):
    """
    Where "this period" begins. Anything dated earlier that is still unpaid
    is carried over: for monthly pay the latest salary day, for daily pay
    today.
    """
    if profile.pay_type == PayType.DAILY:
        return today

    dates = due_dates(
        profile.salary_day,
        first_accrued_through(profile.start_date),
        today,
    )

    return dates[-1] if dates else today


def next_salary_date(profile, today):
    if profile.pay_type != PayType.MONTHLY:
        return None

    start = max(today, first_accrued_through(profile.start_date))
    after = start

    for _ in range(3):
        upcoming = due_dates(
            profile.salary_day,
            after,
            after + timedelta(days=62),
        )

        if upcoming:
            return upcoming[0]

        after += timedelta(days=62)

    return None


# ---------------------------------------------------------------------------
# Matching payments to what is owed
# ---------------------------------------------------------------------------

def settle(entries, payments):
    """
    ``entries`` and ``payments`` are active rows of one person. Returns the
    earning rows (oldest first) with ``paid`` and ``remaining`` set, and the
    totals.
    """
    earnings = sorted(
        (e for e in entries if e.kind in EARNING_KINDS),
        key=lambda e: (e.entry_date, e.id),
    )
    deducted = sum(
        (e.amount for e in entries if e.kind == PayrollEntry.Kind.DEDUCTION),
        ZERO,
    )
    paid_total = sum((p.amount for p in payments), ZERO)

    pool = paid_total + deducted

    for entry in earnings:
        applied = min(entry.amount, pool)
        entry.paid = applied
        entry.remaining = entry.amount - applied
        pool -= applied

    earned = sum((e.amount for e in earnings), ZERO)

    return earnings, {
        "earned": earned,
        "deducted": deducted,
        "paid": paid_total,
        "remaining": sum((e.remaining for e in earnings), ZERO),
        "advance": pool,
    }


def entry_state(entry):
    if entry.remaining == 0:
        return "paid"

    return "partial" if entry.paid > 0 else "unpaid"


def balance(profile, entries, payments, today):
    """Everything the owner wants to know about one person's account."""
    earnings, totals = settle(entries, payments)
    start = current_cycle_start(profile, today)

    previous = sum(
        (e.remaining for e in earnings if e.entry_date < start), ZERO
    )

    for entry in earnings:
        entry.state = entry_state(entry)
        entry.carried_over = entry.remaining > 0 and entry.entry_date < start

    last_payment = max((p.paid_on for p in payments), default=None)

    return {
        "earnings": earnings,
        "entries": entries,
        "payments": payments,
        "pay_type": profile.pay_type,
        "total_earned": totals["earned"],
        "total_deducted": totals["deducted"],
        "total_paid": totals["paid"],
        "remaining": totals["remaining"],
        "previous_remaining": previous,
        "current_remaining": totals["remaining"] - previous,
        "advance": totals["advance"],
        "cycle_start": start,
        "next_salary_date": next_salary_date(profile, today),
        "last_payment_date": last_payment,
    }


def statement(entries, payments):
    """
    The account as a statement: every amount earned, deducted or paid in
    date order, each with what was still owed (or paid ahead) right after
    it. A payment also says how much of it was an advance, meaning paid
    before there was anything owed.
    """
    items = [(e.entry_date, 0, e.id, e) for e in entries] + [
        (p.paid_on, 1, p.id, p) for p in payments
    ]
    items.sort(key=lambda item: item[:3])

    rows = []
    running = ZERO

    for day, _, _, item in items:
        row = {
            "id": item.id,
            "date": day,
            "amount": item.amount,
            "description": "",
            "method": "",
            "reference": "",
            "advance_part": ZERO,
        }

        if isinstance(item, PayrollPayment):
            owed_before = max(running, ZERO)

            running -= item.amount
            row.update(
                type="payment",
                direction="paid",
                description=item.notes or "Payment",
                method=item.method,
                reference=item.reference,
                advance_part=max(item.amount - owed_before, ZERO),
            )

        elif item.kind == PayrollEntry.Kind.DEDUCTION:
            running -= item.amount
            row.update(
                type=item.kind,
                direction="deducted",
                description=item.description or "Deduction",
            )

        else:
            running += item.amount
            row.update(
                type=item.kind,
                direction="earned",
                description=item.description or item.get_kind_display(),
            )

        row["owed_after"] = max(running, ZERO)
        row["advance_after"] = max(-running, ZERO)
        rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# Recording and voiding
# ---------------------------------------------------------------------------

@transaction.atomic
def create_payment(*, staff, business, actor, amount, paid_on, method,
                   reference="", notes=""):
    from apps.finance.services import record_salary_expense

    profile = getattr(staff, "salary_profile", None)

    # Daily wage is paid for work already done, so it can never be paid ahead.
    # Only monthly pay takes an advance.
    if profile is not None and profile.pay_type == PayType.DAILY:
        bal = balance(
            profile,
            list(PayrollEntry.objects.filter(
                staff=staff, status=PayrollEntry.Status.ACTIVE
            )),
            list(PayrollPayment.objects.filter(
                staff=staff, status=PayrollPayment.Status.ACTIVE
            )),
            timezone.localdate(),
        )

        if amount > bal["remaining"]:
            raise Conflict(
                "Daily wage is paid for work already done, not in advance. "
                f"Owed now: {bal['remaining']:.2f}.",
                code="daily_no_advance",
            )

    payment = PayrollPayment.objects.create(
        business=business,
        staff=staff,
        amount=amount,
        paid_on=paid_on,
        method=method,
        reference=reference,
        notes=notes,
        paid_by=actor,
    )

    record_salary_expense(payment)

    return payment


@transaction.atomic
def void_payment(*, payment, actor, reason):
    from apps.finance.services import void_salary_expense

    payment = PayrollPayment.objects.select_for_update().get(pk=payment.pk)

    if payment.status != PayrollPayment.Status.ACTIVE:
        raise Conflict("This payment is already void.", code="invalid_status")

    payment.status = PayrollPayment.Status.VOID
    payment.voided_by = actor
    payment.voided_at = timezone.now()
    payment.void_reason = reason
    payment.save(update_fields=["status", "voided_by", "voided_at", "void_reason"])

    void_salary_expense(payment, actor, reason)

    return payment


@transaction.atomic
def void_entry(*, entry, actor, reason):
    entry = PayrollEntry.objects.select_for_update().get(pk=entry.pk)

    if entry.status != PayrollEntry.Status.ACTIVE:
        raise Conflict("This entry is already void.", code="invalid_status")

    entry.status = PayrollEntry.Status.VOID
    entry.voided_by = actor
    entry.voided_at = timezone.now()
    entry.void_reason = reason
    entry.save(update_fields=["status", "voided_by", "voided_at", "void_reason"])

    return entry


# ---------------------------------------------------------------------------
# Daily attendance
# ---------------------------------------------------------------------------

WORK_FACTOR = {
    WorkDay.Status.PRESENT: Decimal("1"),
    WorkDay.Status.HALF: Decimal("0.5"),
    WorkDay.Status.ABSENT: Decimal("0"),
}


@transaction.atomic
def mark_work_day(*, business, actor, staff, day, status, extra=Decimal("0"),
                  amount=None, note=""):
    """
    Record a day for someone on daily pay and keep their wage in step.

    Present or half credits the day's wage (the daily rate x 1 or 0.5, or
    ``amount`` when given, plus ``extra``); absent credits nothing. Marking
    the same day again changes the wage already credited.
    """
    profile = getattr(staff, "salary_profile", None)

    if profile is None or profile.pay_type != PayType.DAILY:
        raise drf.ValidationError({"staff": "This person is not on daily pay."})

    if not staff.is_active:
        raise drf.ValidationError({"staff": "This team member is inactive."})

    if day > timezone.localdate():
        raise drf.ValidationError({"date": "The date cannot be in the future."})

    extra = Decimal(extra or 0)

    work = (
        WorkDay.objects.select_for_update().filter(staff=staff, work_date=day).first()
    )
    entry = work.entry if work and work.entry else None

    if entry is not None and entry.status != PayrollEntry.Status.ACTIVE:
        entry = None

    if entry is None:
        # A wage typed in by hand for the day is taken over, not doubled.
        entry = PayrollEntry.objects.filter(
            staff=staff,
            kind=PayrollEntry.Kind.WAGE,
            entry_date=day,
            status=PayrollEntry.Status.ACTIVE,
        ).first()

    label = WorkDay.Status(status).label

    if status == WorkDay.Status.ABSENT:
        extra = Decimal("0")

        if entry is not None:
            void_entry(entry=entry, actor=actor, reason="Marked absent")
            entry = None
    else:
        if amount is None:
            if profile.rate is None:
                raise drf.ValidationError({
                    "amount": "Set a daily rate for this person, or enter the amount for the day."
                })

            wage = (profile.rate * WORK_FACTOR[status]).quantize(Decimal("0.01"))
        else:
            wage = Decimal(amount)

        total = wage + extra

        if total <= 0:
            raise drf.ValidationError({"amount": "The day's pay must be more than zero."})

        description = f"{label} {day:%d %b}" + (f" – {note}" if note else "")

        if entry is None:
            entry = PayrollEntry.objects.create(
                business=business,
                staff=staff,
                kind=PayrollEntry.Kind.WAGE,
                entry_date=day,
                amount=total,
                description=description[:200],
                created_by=actor,
            )
        else:
            entry.amount = total
            entry.description = description[:200]
            entry.save(update_fields=["amount", "description", "updated_at"])

    values = {
        "business": business,
        "status": status,
        "extra_amount": extra,
        "note": note,
        "entry": entry,
        "marked_by": actor,
    }

    if work is None:
        work = WorkDay.objects.create(staff=staff, work_date=day, **values)
    else:
        for field, value in values.items():
            setattr(work, field, value)

        work.save()

    return work


# ---------------------------------------------------------------------------
# Incentives
# ---------------------------------------------------------------------------

def month_range(month):
    """First and last day of the month that ``month`` (any date in it) falls in."""
    import calendar

    first = month.replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])

    return first, last


def _rule_applies(rule, user, position_ids):
    if rule.staff_id:
        return rule.staff_id == user.pk

    if rule.position_id:
        return rule.position_id in position_ids

    return True


def _bases(business, user, first, last):
    """What one person did in the month: values and counts the rules pay on."""
    from django.db.models import Count, Sum

    from apps.sales.models import Sale

    confirmed = Sale.objects.filter(
        business=business, status=Sale.Status.CONFIRMED
    )

    sold = confirmed.filter(
        salesperson=user, sale_date__gte=first, sale_date__lte=last
    )
    sales = sold.aggregate(value=Sum("net_total"))["value"] or ZERO
    bikes = sold.filter(sale_type=Sale.SaleType.BIKE).count()

    jobs = confirmed.filter(
        sale_type=Sale.SaleType.MAINTENANCE,
        technician=user,
        job_status=Sale.JobStatus.DONE,
        job_done_at__date__gte=first,
        job_done_at__date__lte=last,
    ).aggregate(count=Count("id"), value=Sum("net_total"))

    return {
        "sales_value": sales,
        "bikes": bikes,
        "jobs": jobs["count"] or 0,
        "jobs_value": jobs["value"] or ZERO,
    }


def incentive_for(business, user, month, rules=None):
    """
    What ``user`` earned as incentive in ``month``: the lines (one per rule
    that paid) and the total. Sales returns are not taken off.
    """
    first, last = month_range(month)

    if rules is None:
        rules = IncentiveRule.objects.filter(business=business, is_active=True)

    position_ids = set()

    if user.position_id:
        position_ids.add(user.position_id)
        position_ids |= set(user.extra_positions.values_list("pk", flat=True))

    bases = _bases(business, user, first, last)
    hundred = Decimal("100")
    lines = []

    for rule in rules:
        if not _rule_applies(rule, user, position_ids):
            continue

        if rule.basis == IncentiveRule.Basis.SALE_PERCENT:
            base, amount = bases["sales_value"], bases["sales_value"] * rule.value / hundred
        elif rule.basis == IncentiveRule.Basis.BIKE_FIXED:
            base, amount = bases["bikes"], bases["bikes"] * rule.value
        elif rule.basis == IncentiveRule.Basis.JOB_FIXED:
            base, amount = bases["jobs"], bases["jobs"] * rule.value
        else:
            base, amount = bases["jobs_value"], bases["jobs_value"] * rule.value / hundred

        amount = amount.quantize(Decimal("0.01"))

        if amount > 0:
            lines.append({
                "rule": rule.pk,
                "name": rule.name,
                "basis": rule.basis,
                "base": str(base),
                "rate": str(rule.value),
                "amount": str(amount),
            })

    total = sum((Decimal(line["amount"]) for line in lines), ZERO)

    return lines, total


def incentive_preview(business, staff, month):
    """One row per person who earned something (or already was paid) in ``month``."""
    first, _ = month_range(month)
    rules = list(IncentiveRule.objects.filter(business=business, is_active=True))

    paid = {
        e.staff_id: e
        for e in PayrollEntry.objects.filter(
            staff__in=staff,
            incentive_month=first,
        ).exclude(status=PayrollEntry.Status.VOID)
    }

    has_profile = set(
        SalaryProfile.objects.filter(user__in=staff).values_list("user_id", flat=True)
    )

    rows = []

    for user in staff.select_related("position"):
        lines, total = incentive_for(business, user, month, rules)
        entry = paid.get(user.pk)

        if not lines and entry is None:
            continue

        rows.append({
            "staff": user.pk,
            "name": user.get_full_name() or user.username,
            "lines": lines,
            "total": str(total),
            "has_pay_profile": user.pk in has_profile,
            "entry": entry.pk if entry else None,
            "applied_amount": str(entry.amount) if entry else None,
        })

    rows.sort(key=lambda r: (-Decimal(r["total"]), r["name"]))

    return rows


@transaction.atomic
def apply_incentives(*, business, staff, actor, month, staff_ids=None):
    """
    Put the month's incentive in each person's pay as a bonus. A month is
    paid once per person; someone without a pay profile is skipped.
    Returns the entries made and who was skipped and why.
    """
    first, last = month_range(month)
    today = timezone.localdate()

    if first > today:
        raise Conflict("That month has not started yet.", code="future_month")

    made, skipped = [], []

    for row in incentive_preview(business, staff, month):
        if staff_ids is not None and row["staff"] not in staff_ids:
            continue

        if row["entry"]:
            skipped.append({"staff": row["staff"], "reason": "already_applied"})
            continue

        total = Decimal(row["total"])

        if total <= 0:
            continue

        user = staff.get(pk=row["staff"])

        if not SalaryProfile.objects.filter(user=user).exists():
            skipped.append({"staff": user.pk, "reason": "no_pay_profile"})
            continue

        detail = "; ".join(f'{l["name"]} {l["amount"]}' for l in row["lines"])
        made.append(PayrollEntry.objects.create(
            business=payroll_business(user),
            staff=user,
            kind=PayrollEntry.Kind.BONUS,
            entry_date=min(last, today),
            amount=total,
            description=f"Incentive {first:%b %Y}: {detail}"[:200],
            incentive_month=first,
            created_by=actor,
        ))

    return made, skipped
