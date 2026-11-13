from decimal import Decimal

from django.utils import timezone

from .models import Expense, Income


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


SALE_INCOME_CATEGORY = {
    "stock": Income.Category.STOCK_SALE,
    "bike": Income.Category.BIKE_SALE,
    "maintenance": Income.Category.MAINTENANCE,
}


def record_sale_income(payment):
    """The income that mirrors one customer payment."""
    sale = payment.sale

    return Income.objects.create(
        business=sale.business,
        category=SALE_INCOME_CATEGORY[sale.sale_type],
        amount=payment.amount,
        income_date=payment.paid_on,
        description=f"Payment for {sale.sale_no}",
        payment_method=payment.method,
        reference=payment.reference,
        sale_payment=payment,
        created_by=payment.received_by,
    )


def void_sale_income(payment, actor, reason):
    Income.objects.filter(
        sale_payment=payment,
        status=Income.Status.ACTIVE,
    ).update(
        status=Income.Status.VOID,
        voided_by=actor,
        voided_at=timezone.now(),
        void_reason=reason,
    )


# ---------------------------------------------------------------------------
# Cash and bank balances
# ---------------------------------------------------------------------------

def method_balance(business, method, as_of):
    """
    What the books say the business holds in ``method`` at the end of ``as_of``:
    the opening balance, plus money received and less money paid after it.
    Voided entries are left out.
    """
    from django.db.models import Sum
    from rest_framework.exceptions import ValidationError

    from .models import Expense, Income, OpeningBalance

    opening = OpeningBalance.objects.filter(business=business, method=method).first()
    base = opening.amount if opening else Decimal("0")

    if opening and as_of < opening.as_of:
        raise ValidationError(
            {"as_of": f"The opening balance is as at {opening.as_of}; choose that date or later."}
        )

    money_in = Income.objects.filter(
        business=business, payment_method=method, status="active", income_date__lte=as_of
    )
    money_out = Expense.objects.filter(
        business=business, payment_method=method, status="active", expense_date__lte=as_of
    )

    if opening:
        money_in = money_in.filter(income_date__gt=opening.as_of)
        money_out = money_out.filter(expense_date__gt=opening.as_of)

    received = money_in.aggregate(t=Sum("amount"))["t"] or Decimal("0")
    paid = money_out.aggregate(t=Sum("amount"))["t"] or Decimal("0")

    return {
        "opening": base,
        "opening_as_of": opening.as_of if opening else None,
        "received": received,
        "paid": paid,
        "expected": base + received - paid,
    }
