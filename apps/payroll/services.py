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

from .models import (
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
