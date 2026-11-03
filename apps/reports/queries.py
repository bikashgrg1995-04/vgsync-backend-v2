"""
Numbers behind the owner reports. Money is computed in the database where
it can be, and returned as Decimal; the views format it as strings.
"""

from collections import defaultdict
from decimal import Decimal

from django.db.models import F, Sum
from django.db.models.functions import TruncMonth

from apps.finance.models import Expense, Income
from apps.inventory.models import StockBalance
from apps.purchases.models import (
    CreditSettlement,
    Purchase,
    PurchasePayment,
)
from apps.sales.models import (
    Sale,
    SaleCreditSettlement,
    SalePayment,
    SaleReturn,
)

ZERO = Decimal("0.00")

# Age of an unpaid document in days: (label, lowest, highest or None).
BUCKETS = (
    ("current", 0, 30),
    ("31_60", 31, 60),
    ("61_90", 61, 90),
    ("over_90", 91, None),
)


def money(value):
    return f"{(value or ZERO):.2f}"


def quantity(value):
    return f"{(value or Decimal('0')):.3f}"


# -- profit and loss -------------------------------------------------------

def _dated(queryset, field, date_from, date_to):
    if date_from:
        queryset = queryset.filter(**{f"{field}__gte": date_from})

    if date_to:
        queryset = queryset.filter(**{f"{field}__lte": date_to})

    return queryset


def _by_month(queryset, field):
    rows = (
        queryset.annotate(month=TruncMonth(field))
        .values("month")
        .annotate(total=Sum("amount"))
        .order_by("month")
    )

    return {row["month"].strftime("%Y-%m"): row["total"] for row in rows}


def profit_and_loss(business, date_from, date_to):
    """
    Cash basis, like the Income and Expenses pages: money that came in
    less refunds paid back to customers, against money that went out less
    refunds received from suppliers.
    """
    income = _dated(
        Income.objects.filter(business=business, status=Income.Status.ACTIVE),
        "income_date", date_from, date_to,
    )
    expenses = _dated(
        Expense.objects.filter(business=business, status=Expense.Status.ACTIVE),
        "expense_date", date_from, date_to,
    )
    sale_refunds = _dated(
        SaleCreditSettlement.objects.filter(
            business=business,
            kind=SaleCreditSettlement.Kind.REFUND,
            status=SaleCreditSettlement.Status.ACTIVE,
        ),
        "settled_on", date_from, date_to,
    )
    purchase_refunds = _dated(
        CreditSettlement.objects.filter(
            supplier__business=business,
            kind=CreditSettlement.Kind.REFUND,
            status=CreditSettlement.Status.ACTIVE,
        ),
        "settled_on", date_from, date_to,
    )

    income_rows = {
        row["category"]: row["total"]
        for row in income.values("category").annotate(total=Sum("amount"))
    }
    expense_rows = {
        row["category"]: row["total"]
        for row in expenses.values("category").annotate(total=Sum("amount"))
    }

    income_total = sum(income_rows.values(), ZERO)
    expense_total = sum(expense_rows.values(), ZERO)
    refunds_out = sale_refunds.aggregate(t=Sum("amount"))["t"] or ZERO
    refunds_in = purchase_refunds.aggregate(t=Sum("amount"))["t"] or ZERO

    net_income = income_total - refunds_out
    net_expenses = expense_total - refunds_in

    months = defaultdict(lambda: {"income": ZERO, "expenses": ZERO})

    for month, total in _by_month(income, "income_date").items():
        months[month]["income"] += total

    for month, total in _by_month(sale_refunds, "settled_on").items():
        months[month]["income"] -= total

    for month, total in _by_month(expenses, "expense_date").items():
        months[month]["expenses"] += total

    for month, total in _by_month(purchase_refunds, "settled_on").items():
        months[month]["expenses"] -= total

    return {
        "income": {
            "total": money(income_total),
            "sale_refunds": money(refunds_out),
            "net": money(net_income),
            "by_category": [
                {"category": key, "total": money(income_rows[key])}
                for key in sorted(income_rows)
            ],
        },
        "expenses": {
            "total": money(expense_total),
            "purchase_refunds": money(refunds_in),
            "net": money(net_expenses),
            "by_category": [
                {"category": key, "total": money(expense_rows[key])}
                for key in sorted(expense_rows)
            ],
        },
        "profit": money(net_income - net_expenses),
        "by_month": [
            {
                "month": month,
                "income": money(figures["income"]),
                "expenses": money(figures["expenses"]),
                "profit": money(figures["income"] - figures["expenses"]),
            }
            for month, figures in sorted(months.items())
        ],
    }


# -- ageing ----------------------------------------------------------------

def _bucket(days):
    for label, low, high in BUCKETS:
        if days >= low and (high is None or days <= high):
            return label

    return BUCKETS[0][0]


def _grouped(queryset, key, field="amount"):
    return {
        row[key]: row["total"]
        for row in queryset.values(key).annotate(total=Sum(field))
    }


def _ageing(rows, today):
    """
    ``rows``: (party id, party name, document date, amount still open).
    Returns the buckets, a per-party table and the total, biggest first.
    """
    totals = {label: ZERO for label, _, _ in BUCKETS}
    parties = {}

    for party_id, name, day, open_amount in rows:
        label = _bucket(max((today - day).days, 0))
        totals[label] += open_amount

        entry = parties.setdefault(
            party_id,
            {"id": party_id, "name": name, "total": ZERO,
             **{key: ZERO for key in totals}},
        )
        entry[label] += open_amount
        entry["total"] += open_amount

    ordered = sorted(parties.values(), key=lambda row: (-row["total"], row["name"]))

    return {
        "total": money(sum(totals.values(), ZERO)),
        "buckets": {label: money(value) for label, value in totals.items()},
        "parties": [
            {key: (value if key in ("id", "name") else money(value))
             for key, value in entry.items()}
            for entry in ordered
        ],
    }


def receivables(business, today):
    """
    What customers still owe on confirmed sales, aged by sale date. EMI
    sales are listed apart: their instalments fall due over time, so their
    age would say nothing.
    """
    sales = list(
        Sale.objects.filter(business=business, status=Sale.Status.CONFIRMED)
        .select_related("customer")
    )
    ids = [sale.pk for sale in sales]

    paid = _grouped(
        SalePayment.objects.filter(sale__in=ids, status=SalePayment.Status.ACTIVE),
        "sale",
    )
    applied = _grouped(
        SaleCreditSettlement.objects.filter(
            sale__in=ids,
            kind=SaleCreditSettlement.Kind.ADJUSTMENT,
            status=SaleCreditSettlement.Status.ACTIVE,
        ),
        "sale",
    )
    returned = _grouped(
        SaleReturn.objects.filter(sale__in=ids, status=SaleReturn.Status.ACTIVE),
        "sale",
        "applied_amount",
    )

    rows = []
    emi_open = ZERO

    for sale in sales:
        open_amount = (
            sale.net_total + sale.finance_charge
            - paid.get(sale.pk, ZERO)
            - applied.get(sale.pk, ZERO)
            - returned.get(sale.pk, ZERO)
        )

        if open_amount <= 0:
            continue

        if sale.payment_mode == Sale.PaymentMode.EMI:
            emi_open += open_amount
            continue

        # A walk-in sale is paid in full, so it always has a customer here.
        rows.append((
            sale.customer_id,
            sale.customer.name if sale.customer_id else "Walk-in",
            sale.sale_date,
            open_amount,
        ))

    report = _ageing(rows, today)
    report["emi_outstanding"] = money(emi_open)

    return report


def payables(business, today):
    """What is owed to suppliers on received (or partly received) purchases, aged by purchase date."""
    purchases = list(
        Purchase.objects.filter(business=business, status__in=Purchase.BILLED)
        .select_related("supplier")
    )
    ids = [purchase.pk for purchase in purchases]

    paid = _grouped(
        PurchasePayment.objects.filter(
            purchase__in=ids, status=PurchasePayment.Status.ACTIVE
        ),
        "purchase",
    )
    applied = _grouped(
        CreditSettlement.objects.filter(
            purchase__in=ids,
            kind=CreditSettlement.Kind.ADJUSTMENT,
            status=CreditSettlement.Status.ACTIVE,
        ),
        "purchase",
    )

    rows = []

    for purchase in purchases:
        open_amount = (
            purchase.net_total
            - paid.get(purchase.pk, ZERO)
            - applied.get(purchase.pk, ZERO)
        )

        if open_amount > 0:
            rows.append((
                purchase.supplier_id,
                purchase.supplier.name,
                purchase.purchase_date,
                open_amount,
            ))

    return _ageing(rows, today)


# -- stock value -----------------------------------------------------------

def stock_value(business):
    """Stock on hand at purchase price, good and damaged kept apart."""
    balances = StockBalance.objects.filter(business=business).select_related(
        "product"
    )

    good = damaged = ZERO
    units_good = units_damaged = Decimal("0")
    products = []
    unpriced = 0

    for balance in balances.order_by("product__stock_no"):
        price = balance.product.purchase_price
        good_value = balance.quantity * price
        damaged_value = balance.damaged_quantity * price

        if not (balance.quantity or balance.damaged_quantity):
            continue

        if not price:
            unpriced += 1

        good += good_value
        damaged += damaged_value
        units_good += balance.quantity
        units_damaged += balance.damaged_quantity

        products.append({
            "product": balance.product_id,
            "stock_no": balance.product.stock_no,
            "name": balance.product.name,
            "purchase_price": money(price),
            "quantity": quantity(balance.quantity),
            "damaged_quantity": quantity(balance.damaged_quantity),
            "good_value": money(good_value),
            "damaged_value": money(damaged_value),
        })

    products.sort(key=lambda row: -Decimal(row["good_value"]))

    return {
        "good_value": money(good),
        "damaged_value": money(damaged),
        "total_value": money(good + damaged),
        "good_units": quantity(units_good),
        "damaged_units": quantity(units_damaged),
        "products_without_price": unpriced,
        "products": products,
    }
