"""
Staff pay as a running account.

Money the business owes a person is recorded as **entries** (monthly
salary, a day's wage, a bonus, a deduction). Money handed over is recorded
as **payments** (a full or part payment, or an advance). What is still owed
is always worked out from the two, never typed in, so it cannot drift.
See ``services.py`` for how payments are matched to entries.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q

MONEY = {"max_digits": 12, "decimal_places": 2}
ZERO = Decimal("0.00")


class PayType(models.TextChoices):
    MONTHLY = "monthly", "Monthly"
    DAILY = "daily", "Daily"


class SalaryProfile(models.Model):
    """
    How one team member is paid.

    * **Monthly**: a fixed ``rate`` (plus ``allowances``) falls due on
      ``salary_day`` of every month, starting from ``start_date``.
    * **Daily**: nothing is fixed. The owner enters the amount earned for
      each day as it happens.

    Changing the rate never alters salary that has already fallen due,
    because each month's entry keeps its own amount.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="salary_profile",
    )
    pay_type = models.CharField(
        max_length=10,
        choices=PayType.choices,
        default=PayType.MONTHLY,
    )
    # Monthly only; empty for daily pay.
    rate = models.DecimalField(**MONEY, null=True, blank=True)
    allowances = models.DecimalField(**MONEY, default=ZERO)
    # Day of the month salary falls due. Short months use their last day.
    salary_day = models.PositiveSmallIntegerField(default=1)
    # Salary falls due on or after this date.
    start_date = models.DateField()
    # Monthly salary has been recorded for every due date up to here. It
    # stops moving while the person is inactive, so a gap is never paid.
    accrued_through = models.DateField()

    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(allowances__gte=0)
                & (Q(rate__isnull=True) | Q(rate__gte=0)),
                name="salary_profile_amounts_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(salary_day__gte=1, salary_day__lte=31),
                name="salary_profile_day_in_month",
            ),
            models.CheckConstraint(
                condition=(
                    Q(pay_type="monthly", rate__isnull=False)
                    | Q(pay_type="daily", rate__isnull=True)
                ),
                name="salary_profile_rate_matches_pay_type",
            ),
        ]

    def __str__(self):
        return f"{self.user} ({self.pay_type})"


class PayrollEntry(models.Model):
    """An amount the business owes the person (or, for a deduction, takes off)."""

    class Kind(models.TextChoices):
        SALARY = "salary", "Monthly salary"
        WAGE = "wage", "Daily wage"
        BONUS = "bonus", "Bonus"
        DEDUCTION = "deduction", "Deduction"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        VOID = "void", "Void"

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="payroll_entries",
    )
    staff = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="payroll_entries",
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    # The day the amount was earned (a wage) or fell due (a salary).
    entry_date = models.DateField()
    amount = models.DecimalField(**MONEY)
    description = models.CharField(max_length=200, blank=True)

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-entry_date", "-id"]
        indexes = [
            models.Index(fields=["staff", "status", "entry_date"]),
            models.Index(fields=["business", "entry_date"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="payroll_entry_amount_positive",
            ),
            # Each month's salary is recorded once.
            models.UniqueConstraint(
                fields=["staff", "entry_date"],
                condition=Q(kind="salary") & ~Q(status="void"),
                name="unique_live_salary_per_due_date",
            ),
            # A day's wage is one entry; edit it to change the amount.
            models.UniqueConstraint(
                fields=["staff", "entry_date"],
                condition=Q(kind="wage") & ~Q(status="void"),
                name="unique_live_wage_per_day",
            ),
        ]

    def __str__(self):
        return f"{self.staff} {self.kind} {self.entry_date} {self.amount}"


class PayrollPayment(models.Model):
    """
    Money paid to the person. It may be a full payment, a part payment, or an
    advance paid before the amount is due. A payment is never edited or
    deleted; a mistake is voided with a reason.
    """

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        BANK = "bank", "Bank transfer"
        ONLINE = "online", "Online"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        VOID = "void", "Void"

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="payroll_payments",
    )
    staff = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="payroll_payments",
    )
    amount = models.DecimalField(**MONEY)
    paid_on = models.DateField()
    method = models.CharField(max_length=10, choices=Method.choices)
    reference = models.CharField(max_length=100, blank=True)
    notes = models.CharField(max_length=200, blank=True)

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-paid_on", "-id"]
        indexes = [
            models.Index(fields=["staff", "status", "paid_on"]),
            models.Index(fields=["business", "paid_on"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="payroll_payment_amount_positive",
            ),
        ]

    def __str__(self):
        return f"{self.staff} paid {self.amount} on {self.paid_on}"
