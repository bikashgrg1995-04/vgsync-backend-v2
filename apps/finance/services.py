from django.utils import timezone

from .models import Expense


def record_salary_expense(payment):
    """The expense that mirrors one payroll payment."""
    name = payment.staff.get_full_name() or payment.staff.username

    return Expense.objects.create(
        business=payment.business,
        category=Expense.Category.SALARY,
        amount=payment.amount,
        expense_date=payment.paid_on,
        description=f"Salary payment to {name}",
        payment_method=payment.method,
        reference=payment.reference,
        payroll_payment=payment,
        created_by=payment.paid_by,
    )


def void_salary_expense(payment, actor, reason):
    Expense.objects.filter(
        payroll_payment=payment,
        status=Expense.Status.ACTIVE,
    ).update(
        status=Expense.Status.VOID,
        voided_by=actor,
        voided_at=timezone.now(),
        void_reason=reason,
    )


def record_purchase_expense(payment):
    """The expense that mirrors one supplier payment."""
    purchase = payment.purchase

    return Expense.objects.create(
        business=purchase.business,
        category=Expense.Category.PURCHASE,
        amount=payment.amount,
        expense_date=payment.paid_on,
        description=f"Payment to {purchase.supplier.name} for {purchase.purchase_no}",
        payment_method=payment.method,
        reference=payment.reference,
        purchase_payment=payment,
        created_by=payment.paid_by,
    )


def void_purchase_expense(payment, actor, reason):
    Expense.objects.filter(
        purchase_payment=payment,
        status=Expense.Status.ACTIVE,
    ).update(
        status=Expense.Status.VOID,
        voided_by=actor,
        voided_at=timezone.now(),
        void_reason=reason,
    )
