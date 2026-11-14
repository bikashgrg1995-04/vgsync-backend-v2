"""
Numbers behind the owner reports. Money is computed in the database where
it can be, and returned as Decimal; the views format it as strings.
"""

from collections import defaultdict
from decimal import Decimal

from django.db.models import Count, F, Sum
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
    SaleItem,
    SaleReturn,
    SaleReturnItem,
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
    financer_open = ZERO

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

        if sale.payment_mode == Sale.PaymentMode.FINANCED:
            # Owed by the finance company, not by the customer's name.
            financer_open += open_amount
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
    report["financer_outstanding"] = money(financer_open)

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


# -- analytics -------------------------------------------------------------

def _ranged(queryset, field, date_from, date_to):
    return _dated(queryset, field, date_from, date_to)


def sales_trend(business, date_from, date_to, group_by):
    """
    Confirmed sales per day, week or month, less the goods returned in the
    same periods. Returns are counted on the day they were made.
    """
    from django.db.models.functions import TruncDay, TruncWeek

    truncate = {"day": TruncDay, "week": TruncWeek, "month": TruncMonth}[group_by]
    fmt = "%Y-%m-%d" if group_by != "month" else "%Y-%m"

    sales = _ranged(
        Sale.objects.filter(business=business, status=Sale.Status.CONFIRMED),
        "sale_date", date_from, date_to,
    )
    returns = _ranged(
        SaleReturn.objects.filter(business=business, status=SaleReturn.Status.ACTIVE),
        "return_date", date_from, date_to,
    )

    periods = defaultdict(
        lambda: {"sales": 0, "total": ZERO, "returns": ZERO}
    )

    for row in (
        sales.annotate(period=truncate("sale_date"))
        .values("period")
        .annotate(count=Count("id"), total=Sum("net_total"))
    ):
        key = row["period"].strftime(fmt)
        periods[key]["sales"] += row["count"]
        periods[key]["total"] += row["total"] or ZERO

    for row in (
        returns.annotate(period=truncate("return_date"))
        .values("period")
        .annotate(
            total=Sum(F("applied_amount") + F("credit_amount")),
        )
    ):
        periods[row["period"].strftime(fmt)]["returns"] += row["total"] or ZERO

    rows = [
        {
            "period": key,
            "sales": figures["sales"],
            "total": money(figures["total"]),
            "returns": money(figures["returns"]),
            "net": money(figures["total"] - figures["returns"]),
        }
        for key, figures in sorted(periods.items())
    ]

    total = sum((p["total"] for p in periods.values()), ZERO)
    returned = sum((p["returns"] for p in periods.values()), ZERO)

    return {
        "group_by": group_by,
        "sales": sum(p["sales"] for p in periods.values()),
        "total": money(total),
        "returns": money(returned),
        "net": money(total - returned),
        "periods": rows,
    }


def top_products(business, date_from, date_to, limit, by):
    """
    Products sold on confirmed sales. ``revenue`` is the line total before
    any discount or VAT on the whole sale. Local products and service
    charges are listed too, under their name.
    """
    items = _ranged(
        SaleItem.objects.filter(
            sale__business=business, sale__status=Sale.Status.CONFIRMED
        ),
        "sale__sale_date", date_from, date_to,
    )

    rows = {}

    for row in items.values(
        "product", "product__stock_no", "product__name",
        "local_product", "description",
    ).annotate(quantity=Sum("quantity"), revenue=Sum("line_total")):
        if row["product"]:
            key = ("product", row["product"])
            name, number = row["product__name"], row["product__stock_no"]
        else:
            key = ("other", row["local_product"] or row["description"])
            name, number = row["description"], ""

        entry = rows.setdefault(key, {
            "product": row["product"],
            "local_product": row["local_product"],
            "stock_no": number,
            "name": name,
            "quantity": Decimal("0"),
            "revenue": ZERO,
            "returned_quantity": Decimal("0"),
        })
        entry["quantity"] += row["quantity"]
        entry["revenue"] += row["revenue"]

    returned = _ranged(
        SaleReturnItem.objects.filter(
            sale_return__business=business,
            sale_return__status=SaleReturn.Status.ACTIVE,
            sale_item__sale__status=Sale.Status.CONFIRMED,
        ),
        "sale_item__sale__sale_date", date_from, date_to,
    )

    for row in returned.values("product").annotate(quantity=Sum("quantity")):
        entry = rows.get(("product", row["product"]))

        if entry is not None:
            entry["returned_quantity"] += row["quantity"]

    order = (lambda e: -e["revenue"]) if by == "value" else (lambda e: -e["quantity"])
    ranked = sorted(rows.values(), key=lambda e: (order(e), e["name"]))[:limit]

    return {
        "by": by,
        "products": [
            {
                "product": entry["product"],
                "local_product": entry["local_product"],
                "stock_no": entry["stock_no"],
                "name": entry["name"],
                "quantity": quantity(entry["quantity"]),
                "returned_quantity": quantity(entry["returned_quantity"]),
                "net_quantity": quantity(entry["quantity"] - entry["returned_quantity"]),
                "revenue": money(entry["revenue"]),
            }
            for entry in ranked
        ],
    }


def top_customers(business, date_from, date_to, limit):
    """Customers by what they bought on confirmed sales, with returns."""
    sales = _ranged(
        Sale.objects.filter(business=business, status=Sale.Status.CONFIRMED),
        "sale_date", date_from, date_to,
    )

    rows = {
        row["customer"]: row
        for row in sales.filter(customer__isnull=False)
        .values("customer", "customer__name")
        .annotate(count=Count("id"), total=Sum("net_total"))
    }

    walk_in = sales.filter(customer__isnull=True).aggregate(
        count=Count("id"), total=Sum("net_total")
    )

    returned = _grouped(
        _ranged(
            SaleReturn.objects.filter(
                business=business, status=SaleReturn.Status.ACTIVE,
                customer__isnull=False,
            ),
            "return_date", date_from, date_to,
        ),
        "customer",
        "applied_amount",
    )
    credited = _grouped(
        _ranged(
            SaleReturn.objects.filter(
                business=business, status=SaleReturn.Status.ACTIVE,
                customer__isnull=False,
            ),
            "return_date", date_from, date_to,
        ),
        "customer",
        "credit_amount",
    )

    ranked = sorted(
        rows.values(), key=lambda r: (-(r["total"] or ZERO), r["customer__name"])
    )[:limit]

    return {
        "customers": [
            {
                "id": row["customer"],
                "name": row["customer__name"],
                "sales": row["count"],
                "total": money(row["total"]),
                "returns": money(
                    returned.get(row["customer"], ZERO)
                    + credited.get(row["customer"], ZERO)
                ),
            }
            for row in ranked
        ],
        "walk_in": {
            "sales": walk_in["count"] or 0,
            "total": money(walk_in["total"]),
        },
    }


def top_suppliers(business, date_from, date_to, limit):
    """Suppliers by what was bought on received (or partly received) purchases."""
    purchases = _ranged(
        Purchase.objects.filter(business=business, status__in=Purchase.BILLED),
        "purchase_date", date_from, date_to,
    )

    rows = (
        purchases.values("supplier", "supplier__name")
        .annotate(count=Count("id"), total=Sum("net_total"))
        .order_by("-total", "supplier__name")[:limit]
    )

    return {
        "suppliers": [
            {
                "id": row["supplier"],
                "name": row["supplier__name"],
                "purchases": row["count"],
                "total": money(row["total"]),
            }
            for row in rows
        ]
    }


def payroll_summary(business, date_from, date_to):
    """
    What staff earned (salary, wages, bonus less deductions) and what was
    paid to them in the period, per person and per month. ``owed_now`` is
    the running balance up to today: all earnings less all payments.
    """
    from django.db.models import Case, When

    from apps.payroll.models import PayrollEntry, PayrollPayment

    entries = PayrollEntry.objects.filter(
        business=business, status=PayrollEntry.Status.ACTIVE
    )
    payments = PayrollPayment.objects.filter(
        business=business, status=PayrollPayment.Status.ACTIVE
    )

    signed = Case(
        When(kind=PayrollEntry.Kind.DEDUCTION, then=-F("amount")),
        default=F("amount"),
        output_field=entries.model._meta.get_field("amount"),
    )

    def earned(qs):
        return {
            row["staff"]: (row["name"], row["total"] or ZERO)
            for row in qs.annotate(signed=signed)
            .values("staff", name=F("staff__username"))
            .annotate(total=Sum("signed"))
        }

    period_entries = _ranged(entries, "entry_date", date_from, date_to)
    period_payments = _ranged(payments, "paid_on", date_from, date_to)

    period_earned = earned(period_entries)
    all_earned = earned(entries)
    period_paid = _grouped(period_payments, "staff")
    all_paid = _grouped(payments, "staff")

    staff_ids = set(period_earned) | set(period_paid) | set(all_earned) | set(all_paid)
    # Full name when there is one, otherwise the login name.
    names = {
        row["pk"]: f'{row["first_name"]} {row["last_name"]}'.strip() or row["username"]
        for row in payments.model._meta.get_field("staff").related_model.objects
        .filter(pk__in=staff_ids)
        .values("pk", "username", "first_name", "last_name")
    }

    people = []

    for staff_id in staff_ids:
        got = period_earned.get(staff_id, ("", ZERO))[1]
        paid = period_paid.get(staff_id, ZERO)
        owed = all_earned.get(staff_id, ("", ZERO))[1] - all_paid.get(staff_id, ZERO)

        people.append({
            "staff": staff_id, "name": names.get(staff_id, ""),
            "earned": got, "paid": paid, "owed_now": owed,
        })

    people.sort(key=lambda p: (-p["paid"], p["name"]))

    months = defaultdict(lambda: {"earned": ZERO, "paid": ZERO})

    for row in (
        period_entries.annotate(signed=signed, month=TruncMonth("entry_date"))
        .values("month")
        .annotate(total=Sum("signed"))
    ):
        months[row["month"].strftime("%Y-%m")]["earned"] += row["total"] or ZERO

    for row in (
        period_payments.annotate(month=TruncMonth("paid_on"))
        .values("month")
        .annotate(total=Sum("amount"))
    ):
        months[row["month"].strftime("%Y-%m")]["paid"] += row["total"] or ZERO

    return {
        "earned": money(sum((p["earned"] for p in people), ZERO)),
        "paid": money(sum((p["paid"] for p in people), ZERO)),
        "owed_now": money(sum((p["owed_now"] for p in people), ZERO)),
        "staff": [
            {
                "staff": p["staff"],
                "name": p["name"],
                "earned": money(p["earned"]),
                "paid": money(p["paid"]),
                "owed_now": money(p["owed_now"]),
            }
            for p in people
        ],
        "by_month": [
            {"month": key, "earned": money(v["earned"]), "paid": money(v["paid"])}
            for key, v in sorted(months.items())
        ],
    }
