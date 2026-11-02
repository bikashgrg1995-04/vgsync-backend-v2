"""
Sales rules in one place: totals, numbering, issuing stock, payments.

Every total on a sale comes from ``calculate_totals`` and is recalculated
whenever the lines change; the client never sends a total.
"""

from decimal import Decimal

from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone
from rest_framework import serializers

from apps.businesses.models import Business
from apps.common.exceptions import Conflict
from apps.inventory.models import StockMovement
from apps.inventory.services.stock import create_stock_movement
from apps.purchases.services import (
    calculate_totals as _calculate_totals,
    money,
    next_number,
    payment_status,
)

from .models import ZERO, Sale, SaleItem, SalePayment

__all__ = ["money", "payment_status"]


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------

def calculate_totals(lines, discount_amount, vat_rate):
    """
    ``lines`` is a list of ``(quantity, unit_price)``. The order is the same
    as for purchases: line totals, minus the discount, VAT on what is left.
    A sale has no freight.
    """
    totals = _calculate_totals(lines, discount_amount, vat_rate, ZERO)
    totals.pop("freight_amount")

    return totals


def paid_total(sale):
    return (
        sale.payments.filter(status=SalePayment.Status.ACTIVE)
        .aggregate(total=Sum("amount"))["total"]
        or ZERO
    )


def resolve_price(item):
    """The price on the line, or the product's sale price when none was sent."""
    price = item.get("unit_price")

    if price is None:
        price = item["product"].sale_price

    if price is None or Decimal(price) <= 0:
        raise serializers.ValidationError({
            "items": (
                f"{item['product'].name} has no sale price. Enter a price "
                "for this line."
            )
        })

    return money(price)


# ---------------------------------------------------------------------------
# Create, edit, cancel
# ---------------------------------------------------------------------------

def _priced(items):
    return [{**item, "unit_price": resolve_price(item)} for item in items]


def _write_items(sale, items, totals):
    SaleItem.objects.bulk_create([
        SaleItem(
            sale=sale,
            product=item["product"],
            quantity=item["quantity"],
            unit_price=item["unit_price"],
            line_total=line_total,
        )
        for item, line_total in zip(items, totals["line_totals"])
    ])


def _header_fields(totals):
    return {k: v for k, v in totals.items() if k != "line_totals"}


@transaction.atomic
def create_sale(*, business, user, items, **header):
    items = _priced(items)

    totals = calculate_totals(
        [(i["quantity"], i["unit_price"]) for i in items],
        header.pop("discount_amount", ZERO),
        header.pop("vat_rate", ZERO),
    )

    sale = Sale.objects.create(
        business=business,
        number=next_number(business, Sale),
        created_by=user,
        **header,
        **_header_fields(totals),
    )

    _write_items(sale, items, totals)

    return sale


def _lock_draft(sale):
    sale = Sale.objects.select_for_update().get(pk=sale.pk)

    if sale.status != Sale.Status.DRAFT:
        raise Conflict(
            f"Only a draft sale can be changed; this one is {sale.status}.",
            code="document_not_editable",
        )

    return sale


@transaction.atomic
def update_sale(*, sale, items=None, **header):
    """
    Change a draft. When ``items`` is given it replaces all lines (safe,
    because a draft has no stock or history attached to its lines).
    """
    sale = _lock_draft(sale)

    for field, value in header.items():
        setattr(sale, field, value)

    if items is None:
        lines = [(i.quantity, i.unit_price) for i in sale.items.all()]
    else:
        items = _priced(items)
        lines = [(i["quantity"], i["unit_price"]) for i in items]

    totals = calculate_totals(lines, sale.discount_amount, sale.vat_rate)
    paid = paid_total(sale)

    if totals["net_total"] < paid:
        raise serializers.ValidationError({
            "items": (
                f"The new total is less than the {paid} already received. "
                "Void a payment first."
            )
        })

    for field, value in _header_fields(totals).items():
        setattr(sale, field, value)

    sale.save()

    if items is not None:
        sale.items.all().delete()
        _write_items(sale, items, totals)

    return sale


@transaction.atomic
def cancel_sale(*, sale, user, reason):
    sale = _lock_draft(sale)

    if sale.payments.filter(status=SalePayment.Status.ACTIVE).exists():
        raise Conflict(
            "Void the payments of this sale before cancelling it.",
            code="payments_exist",
        )

    sale.status = Sale.Status.CANCELLED
    sale.cancelled_by = user
    sale.cancelled_at = timezone.now()
    sale.cancel_reason = reason
    sale.save()

    return sale


# ---------------------------------------------------------------------------
# Confirming: stock goes down
# ---------------------------------------------------------------------------

def next_invoice_number(business):
    # Same lock as ``next_number``: two simultaneous confirmations must not
    # take the same invoice number.
    Business.objects.select_for_update().get(pk=business.pk)

    last = Sale.objects.filter(business=business).aggregate(
        last=Max("invoice_number")
    )["last"]

    return (last or 0) + 1


@transaction.atomic
def confirm_sale(*, sale, user):
    """
    Hand over the goods: take every line out of the business's stock, give
    the sale its invoice number and lock it. Stock moves exactly once; a
    second call is refused, and if any line cannot be issued (for example
    not enough stock) nothing is changed.
    """
    sale = Sale.objects.select_for_update(of=("self",)).select_related(
        "business"
    ).get(pk=sale.pk)

    if sale.status != Sale.Status.DRAFT:
        raise Conflict(
            f"Only a draft sale can be confirmed; this one is {sale.status}.",
            code="invalid_status",
        )

    items = list(sale.items.select_related("product"))

    if not items:
        raise serializers.ValidationError({"items": "The sale has no lines."})

    # Without a customer there is nobody to collect the rest from.
    if sale.customer_id is None and paid_total(sale) < sale.net_total:
        raise Conflict(
            "A walk-in sale must be paid in full. Choose a customer to sell "
            "on credit.",
            code="customer_required",
        )

    invoice_number = next_invoice_number(sale.business)
    reference = f"INV-{invoice_number:06d}"

    for item in items:
        create_stock_movement(
            user=user,
            business=sale.business,
            product=item.product,
            movement_type=StockMovement.MovementType.SALE_ISSUE,
            quantity=item.quantity,
            reference=reference,
            notes=f"Sale {sale.sale_no}",
        )

    sale.status = Sale.Status.CONFIRMED
    sale.invoice_number = invoice_number
    sale.confirmed_by = user
    sale.confirmed_at = timezone.now()
    sale.save()

    return sale


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

@transaction.atomic
def create_payment(*, sale, user, amount, paid_on, method,
                   reference="", notes=""):
    sale = Sale.objects.select_for_update().get(pk=sale.pk)

    if sale.status == Sale.Status.CANCELLED:
        raise Conflict("A cancelled sale cannot take payment.", code="invalid_status")

    paid = paid_total(sale)
    amount = money(amount)

    if paid + amount > sale.net_total:
        raise serializers.ValidationError({
            "amount": f"This is more than is still due ({sale.net_total - paid})."
        })

    return SalePayment.objects.create(
        sale=sale,
        amount=amount,
        paid_on=paid_on,
        method=method,
        reference=reference,
        notes=notes,
        received_by=user,
    )


@transaction.atomic
def void_payment(*, payment, user, reason):
    payment = SalePayment.objects.select_for_update().get(pk=payment.pk)
    sale = Sale.objects.select_for_update().get(pk=payment.sale_id)

    if payment.status != SalePayment.Status.ACTIVE:
        raise Conflict("This payment is already void.", code="invalid_status")

    if (
        sale.status == Sale.Status.CONFIRMED
        and sale.customer_id is None
        and paid_total(sale) - payment.amount < sale.net_total
    ):
        raise Conflict(
            "This walk-in sale would be left unpaid, with no customer to "
            "collect from.",
            code="customer_required",
        )

    payment.status = SalePayment.Status.VOID
    payment.voided_by = user
    payment.voided_at = timezone.now()
    payment.void_reason = reason
    payment.save(update_fields=["status", "voided_by", "voided_at", "void_reason"])

    return payment
