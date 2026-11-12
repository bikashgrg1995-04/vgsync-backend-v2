from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from apps.accounts.scope import scoped_users

from .models import PayrollEntry, PayrollPayment, PayType, SalaryProfile, WorkDay
from .services import first_accrued_through

AMOUNT = {"max_digits": 12, "decimal_places": 2, "min_value": 0}
POSITIVE = {"max_digits": 12, "decimal_places": 2, "min_value": Decimal("0.01")}


def staff_name(user):
    return user.get_full_name() or user.username


class ScopedStaffField(serializers.PrimaryKeyRelatedField):
    """Only the team of the active business can be chosen."""

    def get_queryset(self):
        return scoped_users(self.context["request"].active_business)


# ---------------------------------------------------------------------------
# Pay setup
# ---------------------------------------------------------------------------

class SalaryProfileSerializer(serializers.ModelSerializer):
    staff = serializers.IntegerField(source="user_id", read_only=True)
    staff_name = serializers.SerializerMethodField()

    pay_type = serializers.ChoiceField(choices=PayType.choices)
    rate = serializers.DecimalField(
        **AMOUNT,
        required=False,
        allow_null=True,
        help_text="Monthly salary, or the usual wage for a day on daily pay (optional).",
    )
    allowances = serializers.DecimalField(**AMOUNT, required=False)
    salary_day = serializers.IntegerField(
        min_value=1,
        max_value=31,
        required=False,
        help_text="Day of the month salary falls due (monthly pay).",
    )
    start_date = serializers.DateField(
        required=False,
        help_text="Salary falls due on or after this date. Defaults to today.",
    )

    class Meta:
        model = SalaryProfile
        fields = (
            "staff",
            "staff_name",
            "pay_type",
            "rate",
            "allowances",
            "salary_day",
            "start_date",
            "updated_at",
        )
        read_only_fields = ("updated_at",)

    def get_staff_name(self, obj):
        return staff_name(obj.user)

    def validate(self, attrs):
        instance = self.instance
        pay_type = attrs["pay_type"]

        if pay_type == PayType.MONTHLY:
            rate = attrs.get("rate", instance.rate if instance else None)

            if rate is None:
                raise serializers.ValidationError(
                    {"rate": "Monthly pay needs the monthly salary."}
                )

            attrs["rate"] = rate

        else:
            # A daily rate is the usual wage for a full day. It is optional:
            # without it each day's amount is entered by hand.
            if attrs.get("allowances"):
                raise serializers.ValidationError(
                    {"allowances": "Allowances apply to monthly pay only."}
                )

            attrs["rate"] = attrs.get("rate")
            attrs["allowances"] = 0

        if instance is not None:
            # Dates that already passed stay as they were recorded.
            for locked in ("start_date",):
                if locked in attrs and attrs[locked] != getattr(instance, locked):
                    raise serializers.ValidationError(
                        {locked: "Cannot be changed once pay is set up."}
                    )

        return attrs

    def create(self, validated_data):
        start = validated_data.setdefault("start_date", timezone.localdate())
        validated_data["accrued_through"] = first_accrued_through(start)

        return super().create(validated_data)


# ---------------------------------------------------------------------------
# Entries and payments
# ---------------------------------------------------------------------------

class PayrollEntrySerializer(serializers.ModelSerializer):
    """
    Create a daily wage, a bonus or a deduction. Monthly salary entries are
    created by the system when the salary day arrives.
    """

    staff = ScopedStaffField()
    staff_name = serializers.SerializerMethodField()
    kind = serializers.ChoiceField(choices=PayrollEntry.Kind.choices)
    amount = serializers.DecimalField(**POSITIVE)

    class Meta:
        model = PayrollEntry
        fields = (
            "id",
            "staff",
            "staff_name",
            "kind",
            "entry_date",
            "amount",
            "description",
            "status",
            "voided_at",
            "void_reason",
            "created_at",
        )
        read_only_fields = (
            "id",
            "status",
            "voided_at",
            "void_reason",
            "created_at",
        )

    def get_staff_name(self, obj):
        return staff_name(obj.staff)

    def validate(self, attrs):
        instance = self.instance
        kind = attrs.get("kind", instance.kind if instance else None)

        if instance is not None:
            if instance.status != PayrollEntry.Status.ACTIVE:
                raise serializers.ValidationError("A void entry cannot be edited.")

            if instance.kind == PayrollEntry.Kind.SALARY:
                raise serializers.ValidationError(
                    "Monthly salary is set by the system. Void it and add a "
                    "bonus or deduction to correct it."
                )

            for locked in ("staff", "kind"):
                if locked in attrs and attrs[locked] != getattr(instance, locked):
                    raise serializers.ValidationError(
                        {locked: "Cannot be changed. Void the entry and add a new one."}
                    )

        if kind == PayrollEntry.Kind.SALARY:
            raise serializers.ValidationError(
                {"kind": "Monthly salary is added by the system."}
            )

        staff = attrs.get("staff", instance.staff if instance else None)
        profile = getattr(staff, "salary_profile", None)

        if profile is None:
            raise serializers.ValidationError(
                {"staff": "Set this person's pay first."}
            )

        if kind == PayrollEntry.Kind.WAGE and profile.pay_type != PayType.DAILY:
            raise serializers.ValidationError(
                {"kind": "Daily wages are for staff on daily pay."}
            )

        if not staff.is_active:
            raise serializers.ValidationError({"staff": "This team member is inactive."})

        entry_date = attrs.get("entry_date", instance.entry_date if instance else None)

        if kind == PayrollEntry.Kind.WAGE:
            clash = PayrollEntry.objects.filter(
                staff=staff,
                kind=PayrollEntry.Kind.WAGE,
                entry_date=entry_date,
                status=PayrollEntry.Status.ACTIVE,
            )

            if instance:
                clash = clash.exclude(pk=instance.pk)

            if clash.exists():
                raise serializers.ValidationError({
                    "entry_date": (
                        "A wage is already entered for this day. Edit it "
                        "instead."
                    )
                })

        if entry_date and entry_date > timezone.localdate():
            raise serializers.ValidationError(
                {"entry_date": "The date cannot be in the future."}
            )

        return attrs

    def create(self, validated_data):
        request = self.context["request"]

        return PayrollEntry.objects.create(
            business=request.active_business,
            created_by=request.user,
            **validated_data,
        )


class PayrollPaymentSerializer(serializers.ModelSerializer):
    staff = ScopedStaffField()
    staff_name = serializers.SerializerMethodField()
    amount = serializers.DecimalField(**POSITIVE)
    paid_on = serializers.DateField(required=False)

    class Meta:
        model = PayrollPayment
        fields = (
            "id",
            "staff",
            "staff_name",
            "amount",
            "paid_on",
            "method",
            "reference",
            "notes",
            "status",
            "voided_at",
            "void_reason",
            "created_at",
        )
        read_only_fields = ("id", "status", "voided_at", "void_reason", "created_at")

    def get_staff_name(self, obj):
        return staff_name(obj.staff)

    def validate_paid_on(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("Payment date cannot be in the future.")

        return value

    def validate_staff(self, staff):
        if getattr(staff, "salary_profile", None) is None:
            raise serializers.ValidationError("Set this person's pay first.")

        return staff


class VoidSerializer(serializers.Serializer):
    reason = serializers.CharField(allow_blank=False, trim_whitespace=True)


# ---------------------------------------------------------------------------
# Balances (read-only; built from the figures in ``services.balance``)
# ---------------------------------------------------------------------------

class MoneyField(serializers.DecimalField):
    def __init__(self, **kwargs):
        super().__init__(max_digits=14, decimal_places=2, **kwargs)


class UnsettledEntrySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    kind = serializers.CharField()
    entry_date = serializers.DateField()
    description = serializers.CharField(allow_blank=True)
    amount = MoneyField()
    paid = MoneyField()
    remaining = MoneyField()
    state = serializers.CharField()
    carried_over = serializers.BooleanField()


class BalanceSerializer(serializers.Serializer):
    """Money is returned as strings, like everywhere else in the API."""

    staff = serializers.IntegerField()
    staff_name = serializers.CharField()
    is_active = serializers.BooleanField()
    pay_type = serializers.CharField()
    total_earned = MoneyField()
    total_deducted = MoneyField()
    total_paid = MoneyField()
    remaining = MoneyField()
    previous_remaining = MoneyField()
    current_remaining = MoneyField()
    advance = MoneyField()
    cycle_start = serializers.DateField()
    next_salary_date = serializers.DateField(allow_null=True)
    last_payment_date = serializers.DateField(allow_null=True)


class BalanceDetailSerializer(BalanceSerializer):
    unsettled = UnsettledEntrySerializer(many=True)
    recent_payments = PayrollPaymentSerializer(many=True)


class StatementRowSerializer(serializers.Serializer):
    """One line of a person's statement; see ``services.statement``."""

    id = serializers.IntegerField()
    date = serializers.DateField()
    type = serializers.CharField()
    direction = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    amount = MoneyField()
    method = serializers.CharField(allow_blank=True)
    reference = serializers.CharField(allow_blank=True)
    advance_part = MoneyField()
    owed_after = MoneyField()
    advance_after = MoneyField()


class WorkMarkSerializer(serializers.Serializer):
    """One person's mark for the day."""

    staff = serializers.IntegerField()
    status = serializers.ChoiceField(choices=WorkDay.Status.choices)
    extra = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, default=0
    )
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, allow_null=True,
        help_text="The day's wage when it is not the usual rate.",
    )
    note = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")


class WorkSheetSerializer(serializers.Serializer):
    date = serializers.DateField()
    marks = WorkMarkSerializer(many=True, allow_empty=False, max_length=300)
