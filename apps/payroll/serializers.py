from django.utils import timezone
from rest_framework import serializers

from apps.accounts.scope import scoped_users
from apps.common.exceptions import Conflict

from .models import PayrollRecord, PayType, SalaryProfile
from .services import overlapping_records

AMOUNT = {"max_digits": 12, "decimal_places": 2, "min_value": 0}
UNITS = {"max_digits": 7, "decimal_places": 2, "min_value": 0}


def staff_name(user):
    return user.get_full_name() or user.username


class ScopedStaffField(serializers.PrimaryKeyRelatedField):
    """Only the team of the active business can be chosen."""

    def get_queryset(self):
        return scoped_users(self.context["request"].active_business)


class SalaryProfileSerializer(serializers.ModelSerializer):
    staff = serializers.IntegerField(source="user_id", read_only=True)
    staff_name = serializers.SerializerMethodField()

    pay_type = serializers.ChoiceField(
        choices=PayType.choices,
        required=False,
        default=PayType.MONTHLY,
    )
    rate = serializers.DecimalField(
        **AMOUNT,
        help_text="Amount per month, per day or per hour, by pay type.",
    )
    allowances = serializers.DecimalField(**AMOUNT, required=False)

    class Meta:
        model = SalaryProfile
        fields = (
            "staff",
            "staff_name",
            "pay_type",
            "rate",
            "allowances",
            "updated_at",
        )
        read_only_fields = ("updated_at",)

    def get_staff_name(self, obj):
        return staff_name(obj.user)


class PayrollRecordSerializer(serializers.ModelSerializer):
    """
    Read and write shape of a payroll record. ``net_salary`` and everything
    about payment and voiding are read-only: totals are calculated here on
    the server, and a record changes state only through ``pay`` and ``void``.
    """

    staff = ScopedStaffField(required=True)
    staff_name = serializers.SerializerMethodField()

    # Optional on create: pay type, rate and allowances default to the
    # staff member's salary. ``basic_salary`` is calculated, never sent.
    pay_type = serializers.ChoiceField(choices=PayType.choices, required=False)
    rate = serializers.DecimalField(**AMOUNT, required=False)
    units = serializers.DecimalField(
        **UNITS,
        required=False,
        allow_null=True,
        help_text="Days or hours worked. Only for daily or hourly pay.",
    )
    allowances = serializers.DecimalField(**AMOUNT, required=False)
    bonus = serializers.DecimalField(**AMOUNT, required=False)
    deductions = serializers.DecimalField(**AMOUNT, required=False)
    advance_deduction = serializers.DecimalField(**AMOUNT, required=False)

    class Meta:
        model = PayrollRecord
        fields = (
            "id",
            "staff",
            "staff_name",
            "period_start",
            "period_end",
            "pay_type",
            "rate",
            "units",
            "basic_salary",
            "allowances",
            "bonus",
            "deductions",
            "advance_deduction",
            "net_salary",
            "status",
            "paid_on",
            "payment_method",
            "payment_reference",
            "notes",
            "paid_by",
            "voided_by",
            "voided_at",
            "void_reason",
            "created_by",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "basic_salary",
            "net_salary",
            "status",
            "paid_on",
            "payment_method",
            "payment_reference",
            "paid_by",
            "voided_by",
            "voided_at",
            "void_reason",
            "created_by",
            "created_at",
            "updated_at",
        )

    def get_staff_name(self, obj):
        return staff_name(obj.staff)

    def validate(self, attrs):
        instance = self.instance

        if instance is not None:
            if instance.status != PayrollRecord.Status.DRAFT:
                raise Conflict(
                    "Only a draft can be edited. Void a paid record and "
                    "create a new one to correct it.",
                    code="document_not_editable",
                )

            # Who and when are fixed once the record exists.
            for locked in ("staff", "period_start", "period_end"):
                if locked in attrs and attrs[locked] != getattr(instance, locked):
                    raise serializers.ValidationError(
                        {locked: "Cannot be changed. Delete the draft and create a new one."}
                    )

        staff = attrs.get("staff", instance.staff if instance else None)
        period_start = attrs.get(
            "period_start", instance.period_start if instance else None
        )
        period_end = attrs.get(
            "period_end", instance.period_end if instance else None
        )

        if period_start is None or period_end is None:
            raise serializers.ValidationError(
                {"period_start": "Period start and end are required."}
            )

        if period_end < period_start:
            raise serializers.ValidationError(
                {"period_end": "Period end cannot be before period start."}
            )

        if instance is None:
            if not staff.is_active:
                raise serializers.ValidationError(
                    {"staff": "This team member is inactive."}
                )

            if overlapping_records(staff, period_start, period_end).exists():
                raise serializers.ValidationError({
                    "period_start": (
                        "This person already has payroll that overlaps this "
                        "period. Void it first if it was a mistake."
                    )
                })

            # Pay defaults to the agreed salary.
            profile = getattr(staff, "salary_profile", None)

            if "rate" not in attrs:
                if profile is None:
                    raise serializers.ValidationError({
                        "rate": (
                            "Set a salary for this person first, or enter "
                            "the pay type and rate."
                        )
                    })

                attrs["rate"] = profile.rate

                # The profile's pay type goes with its rate; a different
                # type cannot reuse a rate meant for another unit.
                if attrs.get("pay_type", profile.pay_type) != profile.pay_type:
                    raise serializers.ValidationError(
                        {"rate": "Enter the rate for this pay type."}
                    )

                attrs["pay_type"] = profile.pay_type

            if "allowances" not in attrs and profile is not None:
                attrs["allowances"] = profile.allowances

        def current(name, default):
            if name in attrs:
                return attrs[name]

            return getattr(instance, name) if instance else default

        pay_type = current("pay_type", PayType.MONTHLY)
        units = current("units", None)

        if pay_type == PayType.MONTHLY:
            if "units" not in attrs:
                # Switching a draft to monthly drops the days or hours.
                attrs["units"] = units = None

            if units is not None:
                raise serializers.ValidationError(
                    {"units": "Units apply only to daily or hourly pay."}
                )
        else:
            units = units or 0
            attrs["units"] = units

            days = (period_end - period_start).days + 1
            limit = days if pay_type == PayType.DAILY else days * 24
            noun = "days" if pay_type == PayType.DAILY else "hours"

            if units > limit:
                raise serializers.ValidationError({
                    "units": f"More {noun} than the period has ({limit})."
                })

        # Check the result on the same figures the model will save.
        probe = PayrollRecord(
            pay_type=pay_type,
            rate=current("rate", 0),
            units=units,
            allowances=current("allowances", 0),
            bonus=current("bonus", 0),
            deductions=current("deductions", 0),
            advance_deduction=current("advance_deduction", 0),
        )

        if probe.calculate_net() < 0:
            raise serializers.ValidationError({
                "deductions": "Deductions and advance recovery cannot exceed the pay."
            })

        return attrs

    def create(self, validated_data):
        request = self.context["request"]

        return PayrollRecord.objects.create(
            business=request.active_business,
            created_by=request.user,
            **validated_data,
        )


class PaySerializer(serializers.Serializer):
    paid_on = serializers.DateField(required=False)
    payment_method = serializers.ChoiceField(choices=PayrollRecord.PaymentMethod.choices)
    payment_reference = serializers.CharField(
        required=False, allow_blank=True, max_length=100
    )

    def validate_paid_on(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("Payment date cannot be in the future.")

        return value


class VoidSerializer(serializers.Serializer):
    reason = serializers.CharField(allow_blank=False, trim_whitespace=True)


class GenerateSerializer(serializers.Serializer):
    period_start = serializers.DateField()
    period_end = serializers.DateField()

    def validate(self, attrs):
        if attrs["period_end"] < attrs["period_start"]:
            raise serializers.ValidationError(
                {"period_end": "Period end cannot be before period start."}
            )

        return attrs
