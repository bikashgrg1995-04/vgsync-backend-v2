from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from apps.common.exceptions import Conflict

from .models import Expense

MANUAL_CATEGORIES = [
    (value, label)
    for value, label in Expense.Category.choices
    if value != Expense.Category.SALARY
]


class ExpenseSerializer(serializers.ModelSerializer):
    category = serializers.ChoiceField(choices=Expense.Category.choices)
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0.01")
    )
    expense_date = serializers.DateField(required=False)
    payroll_payment = serializers.IntegerField(
        source="payroll_payment_id", read_only=True
    )
    purchase_payment = serializers.IntegerField(
        source="purchase_payment_id", read_only=True
    )

    class Meta:
        model = Expense
        fields = (
            "id",
            "category",
            "amount",
            "expense_date",
            "description",
            "payment_method",
            "reference",
            "payroll_payment",
            "purchase_payment",
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

    def validate_category(self, value):
        if value == Expense.Category.SALARY:
            raise serializers.ValidationError(
                "Salary expenses are created by recording a payroll payment."
            )

        if value == Expense.Category.PURCHASE:
            raise serializers.ValidationError(
                "Purchase expenses are created by paying a supplier."
            )

        return value

    def validate_expense_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("The date cannot be in the future.")

        return value

    def validate(self, attrs):
        instance = self.instance

        if instance is not None:
            if instance.payroll_payment_id or instance.purchase_payment_id:
                raise Conflict(
                    "This expense comes from a payment. Void the payment "
                    "to remove it.",
                    code="payment_expense_locked",
                )

            if instance.status != Expense.Status.ACTIVE:
                raise Conflict(
                    "A void expense cannot be edited.", code="invalid_status"
                )

        return attrs


class VoidSerializer(serializers.Serializer):
    reason = serializers.CharField(allow_blank=False, trim_whitespace=True)
