from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import F, Q

MONEY = {"max_digits": 12, "decimal_places": 2}
UNITS = {"max_digits": 7, "decimal_places": 2}
ZERO = Decimal("0.00")
CENT = Decimal("0.01")


class PayType(models.TextChoices):
    """How a person is paid; ``rate`` is per month, per day or per hour."""

    MONTHLY = "monthly", "Monthly"
    DAILY = "daily", "Daily"
    HOURLY = "hourly", "Hourly"


class SalaryProfile(models.Model):
    """
    The agreed pay of one team member: a pay type and a rate (per month,
    per day or per hour). It is the default used when payroll records are
    created; changing it never alters past records, because each record
    keeps its own copy of the amounts. ``allowances`` is a fixed amount
    added to every record whatever the pay type.
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
    rate = models.DecimalField(**MONEY)
    allowances = models.DecimalField(**MONEY, default=ZERO)

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
                condition=Q(rate__gte=0) & Q(allowances__gte=0),
                name="salary_profile_amounts_not_negative",
            ),
        ]

    def __str__(self):
        return f"{self.user} - {self.rate} ({self.pay_type})"


class PayrollRecord(models.Model):
    """
    Pay for one team member for one period.

    The net salary is always calculated on the server from the amounts on
    the record. A paid record is locked; a mistake is corrected by voiding
    it (with a reason) and creating a new one, so payment history is never
    rewritten or deleted.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PAID = "paid", "Paid"
        VOID = "void", "Void"

    class PaymentMethod(models.TextChoices):
        CASH = "cash", "Cash"
        BANK = "bank", "Bank transfer"
        ONLINE = "online", "Online"

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="payroll_records",
    )
    staff = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="payroll_records",
    )

    period_start = models.DateField()
    period_end = models.DateField()

    pay_type = models.CharField(
        max_length=10,
        choices=PayType.choices,
        default=PayType.MONTHLY,
    )
    rate = models.DecimalField(**MONEY, default=ZERO)
    # Days or hours worked in the period. Empty for monthly pay.
    units = models.DecimalField(**UNITS, null=True, blank=True)
    # Calculated: the rate for monthly pay, otherwise rate x units.
    basic_salary = models.DecimalField(**MONEY, editable=False, default=ZERO)
    allowances = models.DecimalField(**MONEY, default=ZERO)
    bonus = models.DecimalField(**MONEY, default=ZERO)
    deductions = models.DecimalField(**MONEY, default=ZERO)
    advance_deduction = models.DecimalField(**MONEY, default=ZERO)
    net_salary = models.DecimalField(**MONEY, editable=False, default=ZERO)

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.DRAFT,
    )

    paid_on = models.DateField(null=True, blank=True)
    payment_method = models.CharField(
        max_length=10,
        choices=PaymentMethod.choices,
        blank=True,
    )
    payment_reference = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
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
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-period_start", "staff__username"]
        indexes = [
            models.Index(fields=["business", "status", "period_start"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(period_end__gte=F("period_start")),
                name="payroll_period_end_not_before_start",
            ),
            models.CheckConstraint(
                condition=(
                    Q(rate__gte=0)
                    & Q(basic_salary__gte=0)
                    & Q(allowances__gte=0)
                    & Q(bonus__gte=0)
                    & Q(deductions__gte=0)
                    & Q(advance_deduction__gte=0)
                    & Q(net_salary__gte=0)
                ),
                name="payroll_amounts_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(units__isnull=True) | Q(units__gte=0),
                name="payroll_units_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(units__isnull=True) | ~Q(pay_type="monthly"),
                name="payroll_monthly_has_no_units",
            ),
            # One live record per person per exact period. Overlaps are
            # rejected by the serializer; this is the database backstop.
            models.UniqueConstraint(
                fields=["staff", "period_start", "period_end"],
                condition=~Q(status="void"),
                name="unique_live_payroll_per_staff_period",
            ),
        ]

    def calculate_basic(self):
        if self.pay_type == PayType.MONTHLY:
            return self.rate

        return (self.rate * (self.units or ZERO)).quantize(CENT)

    def calculate_net(self):
        return (
            self.calculate_basic()
            + self.allowances
            + self.bonus
            - self.deductions
            - self.advance_deduction
        )

    def save(self, *args, **kwargs):
        self.basic_salary = self.calculate_basic()
        self.net_salary = self.calculate_net()

        update_fields = kwargs.get("update_fields")

        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {
                "basic_salary",
                "net_salary",
            }

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.staff} {self.period_start}..{self.period_end}"
