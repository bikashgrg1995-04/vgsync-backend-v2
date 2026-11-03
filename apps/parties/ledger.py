"""
The statement of a customer or a supplier: every transaction with them in
date order, with a running balance.

The balance is the money still to be settled between the two sides, worked
out from the records themselves (never stored, so it cannot drift):

* ``debit`` is what adds to it: a sale or a purchase, and credit that was
  turned into a cash refund or into replacement goods.
* ``credit`` is what takes off it: a payment, and returned goods.

A positive balance means money is still to be paid (by the customer to you,
or by you to the supplier). A negative balance means the other way round:
credit owed back. Setting credit against another sale or purchase moves no
money between the two sides, so it is shown with no effect on the balance.
Voided records and cancelled purchases are left out.
"""

from decimal import Decimal

from django.db.models import Sum

from apps.purchases import models as purchases
from apps.sales import models as sales

ZERO = Decimal("0.00")

# Order within one day: what is owed first, then what settles it.
CHARGE, PAYMENT, RETURN, CREDIT_USE = 0, 1, 2, 3

CUSTOMER_TYPES = ("sale", "payment", "return", "refund", "replacement", "adjustment")
SUPPLIER_TYPES = ("purchase", "payment", "return", "refund", "replacement", "adjustment")


def _row(date, rank, doc_id, kind, ref, description, debit=ZERO, credit=ZERO,
         document=None):
    return {
        "date": date,
        "rank": rank,
        "doc_id": doc_id,
        "type": kind,
        "reference": ref,
        "description": description,
        "debit": debit,
        "credit": credit,
        "document": document or kind,
    }


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------

def customer_rows(customer):
    rows = []

    for sale in sales.Sale.objects.filter(
        customer=customer, status=sales.Sale.Status.CONFIRMED
    ):
        rows.append(_row(
            sale.sale_date, CHARGE, sale.pk, "sale", sale.invoice_no,
            f"{sale.get_sale_type_display()} {sale.sale_no}",
            debit=sale.amount_due, document="sale",
        ))

    for payment in sales.SalePayment.objects.filter(
        sale__customer=customer, status=sales.SalePayment.Status.ACTIVE
    ).select_related("sale"):
        rows.append(_row(
            payment.paid_on, PAYMENT, payment.pk, "payment",
            payment.reference or payment.sale.sale_no,
            f"Payment for {payment.sale.sale_no} ({payment.get_method_display()})",
            credit=payment.amount,
        ))

    for ret in sales.SaleReturn.objects.filter(
        customer=customer, status=sales.SaleReturn.Status.ACTIVE
    ).select_related("sale").annotate(value=Sum("items__credit_amount")):
        rows.append(_row(
            ret.return_date, RETURN, ret.pk, "return", ret.return_no,
            f"Goods returned from {ret.sale.sale_no}",
            credit=ret.value or ZERO,
        ))

    for item in sales.SaleCreditSettlement.objects.filter(
        customer=customer, status=sales.SaleCreditSettlement.Status.ACTIVE
    ).select_related("sale"):
        kind = item.kind

        if kind == sales.SaleCreditSettlement.Kind.REFUND:
            rows.append(_row(
                item.settled_on, CREDIT_USE, item.pk, "refund",
                item.reference or "", f"Refund paid ({item.method})",
                debit=item.amount, document="credit",
            ))
        elif kind == sales.SaleCreditSettlement.Kind.REPLACEMENT:
            state = "delivered" if item.delivered_on else "to be delivered"
            rows.append(_row(
                item.settled_on, CREDIT_USE, item.pk, "replacement",
                f"RPL-{item.pk:06d}", f"Replacement goods ({state})",
                debit=item.amount, document="credit",
            ))
        else:
            rows.append(_row(
                item.settled_on, CREDIT_USE, item.pk, "adjustment",
                item.sale.sale_no,
                f"{item.amount:.2f} of credit set against {item.sale.sale_no}",
                document="credit",
            ))

    return rows


# ---------------------------------------------------------------------------
# Supplier
# ---------------------------------------------------------------------------

def supplier_rows(supplier):
    rows = []

    for purchase in purchases.Purchase.objects.filter(supplier=supplier).exclude(
        status=purchases.Purchase.Status.CANCELLED
    ):
        state = {
            purchases.Purchase.Status.DRAFT: "ordered",
            purchases.Purchase.Status.PARTIAL: "partly received",
        }.get(purchase.status, "received")
        rows.append(_row(
            purchase.purchase_date, CHARGE, purchase.pk, "purchase",
            purchase.bill_no or purchase.purchase_no,
            f"Purchase {purchase.purchase_no} ({state})",
            debit=purchase.net_total,
        ))

    for payment in purchases.PurchasePayment.objects.filter(
        purchase__supplier=supplier, status=purchases.PurchasePayment.Status.ACTIVE
    ).select_related("purchase"):
        rows.append(_row(
            payment.paid_on, PAYMENT, payment.pk, "payment",
            payment.reference or payment.purchase.purchase_no,
            f"Payment for {payment.purchase.purchase_no} ({payment.get_method_display()})",
            credit=payment.amount,
        ))

    for ret in purchases.PurchaseReturn.objects.filter(
        supplier=supplier, status=purchases.PurchaseReturn.Status.ACTIVE
    ).select_related("purchase"):
        rows.append(_row(
            ret.return_date, RETURN, ret.pk, "return", ret.return_no,
            f"Goods returned from {ret.purchase.purchase_no}",
            credit=ret.credit_amount,
        ))

    for item in purchases.CreditSettlement.objects.filter(
        supplier=supplier, status=purchases.CreditSettlement.Status.ACTIVE
    ).select_related("purchase"):
        kind = item.kind

        if kind == purchases.CreditSettlement.Kind.REFUND:
            rows.append(_row(
                item.settled_on, CREDIT_USE, item.pk, "refund",
                item.reference or "", "Refund received",
                debit=item.amount, document="credit",
            ))
        elif kind == purchases.CreditSettlement.Kind.REPLACEMENT:
            state = "received" if item.received_on else "to be received"
            rows.append(_row(
                item.settled_on, CREDIT_USE, item.pk, "replacement",
                f"RPL-{item.pk:06d}", f"Replacement goods ({state})",
                debit=item.amount, document="credit",
            ))
        else:
            rows.append(_row(
                item.settled_on, CREDIT_USE, item.pk, "adjustment",
                item.purchase.purchase_no,
                f"{item.amount:.2f} of credit applied to {item.purchase.purchase_no}",
                document="credit",
            ))

    return rows


# ---------------------------------------------------------------------------
# Statement
# ---------------------------------------------------------------------------

def build_statement(rows, date_from=None, date_to=None, types=None):
    """
    Put ``rows`` in order, work out the running balance, and cut out the
    period and the types asked for. The balance always counts every record,
    so filtering by type never makes it wrong.
    """
    rows = sorted(rows, key=lambda r: (r["date"], r["rank"], r["doc_id"]))

    opening = ZERO
    closing = ZERO
    balance = ZERO
    shown = []
    total_debit = ZERO
    total_credit = ZERO

    for row in rows:
        if date_to is not None and row["date"] > date_to:
            break

        balance += row["debit"] - row["credit"]
        closing = balance

        if date_from is not None and row["date"] < date_from:
            opening = balance
            continue

        row["balance"] = balance

        if types and row["type"] not in types:
            continue

        total_debit += row["debit"]
        total_credit += row["credit"]
        shown.append(row)

    return {
        "opening_balance": opening,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "closing_balance": closing,
        "rows": shown,
    }


def money_string(value):
    return f"{value:.2f}"


def render_row(row):
    return {
        "date": row["date"],
        "type": row["type"],
        "reference": row["reference"],
        "description": row["description"],
        "debit": money_string(row["debit"]),
        "credit": money_string(row["credit"]),
        "balance": money_string(row["balance"]),
        "document": row["document"],
        "document_id": row["doc_id"],
    }
