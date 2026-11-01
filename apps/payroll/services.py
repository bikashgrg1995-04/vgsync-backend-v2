"""
Payroll state changes. Views call these so the rules live in one place.
"""

from django.db import transaction
from django.utils import timezone

from apps.common.exceptions import Conflict

from .models import PayrollRecord, PayType, SalaryProfile


def overlapping_records(staff, period_start, period_end, exclude=None):
    """Live (not void) records of this person that overlap the period."""
    records = PayrollRecord.objects.filter(
        staff=staff,
        period_start__lte=period_end,
        period_end__gte=period_start,
    ).exclude(status=PayrollRecord.Status.VOID)

    if exclude is not None:
        records = records.exclude(pk=exclude.pk)

    return records


@transaction.atomic
def generate_drafts(*, business, staff_queryset, period_start, period_end, actor):
    """
    Create a draft for every active team member who has a salary and does
    not already have a live record overlapping the period. Returns the
    created records and a list of who was skipped and why.
    """
    created = []
    skipped = []

    staff_list = (
        staff_queryset
        .filter(is_active=True)
        .select_related("salary_profile")
        .order_by("username")
    )

    for staff in staff_list:
        try:
            profile = staff.salary_profile
        except SalaryProfile.DoesNotExist:
            skipped.append({"staff": staff.pk, "reason": "no_salary"})
            continue

        if overlapping_records(staff, period_start, period_end).exists():
            skipped.append({"staff": staff.pk, "reason": "already_exists"})
            continue

        created.append(
            PayrollRecord.objects.create(
                business=business,
                staff=staff,
                period_start=period_start,
                period_end=period_end,
                pay_type=profile.pay_type,
                rate=profile.rate,
                # Days or hours are entered after the period, before paying.
                units=None if profile.pay_type == PayType.MONTHLY else 0,
                allowances=profile.allowances,
                created_by=actor,
            )
        )

    return created, skipped


@transaction.atomic
def mark_paid(*, record, actor, paid_on, payment_method, payment_reference=""):
    record = PayrollRecord.objects.select_for_update().get(pk=record.pk)

    if record.status != PayrollRecord.Status.DRAFT:
        raise Conflict(
            f"Only a draft can be paid; this record is {record.status}.",
            code="invalid_status",
        )

    if record.pay_type != PayType.MONTHLY and not record.units:
        unit = "days" if record.pay_type == PayType.DAILY else "hours"

        raise Conflict(
            f"Enter the {unit} worked before paying. Delete the draft if "
            "there is nothing to pay.",
            code="units_required",
        )

    record.status = PayrollRecord.Status.PAID
    record.paid_on = paid_on
    record.payment_method = payment_method
    record.payment_reference = payment_reference
    record.paid_by = actor
    record.save()

    return record


@transaction.atomic
def void_record(*, record, actor, reason):
    record = PayrollRecord.objects.select_for_update().get(pk=record.pk)

    if record.status != PayrollRecord.Status.PAID:
        raise Conflict(
            "Only a paid record can be voided. Delete a draft instead.",
            code="invalid_status",
        )

    record.status = PayrollRecord.Status.VOID
    record.voided_by = actor
    record.voided_at = timezone.now()
    record.void_reason = reason
    record.save()

    return record
