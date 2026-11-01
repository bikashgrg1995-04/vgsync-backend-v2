"""
Purchase rules in one place: totals, numbering, receiving stock, payments.

Money is never calculated in model signals or in the client. Every total on
a purchase comes from ``calculate_totals`` and is recalculated whenever the
lines change.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone
from rest_framework import serializers

from apps.businesses.models import Business
from apps.common.exceptions import Conflict
from apps.inventory.models import StockMovement
from apps.inventory.services.stock import create_stock_movement

from .models import Purchase, PurchaseItem, PurchasePayment, ZERO

CENT = Decimal("0.01")
HUNDRED = Decimal("100")


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------

def calculate_totals(lines, discount_amount, vat_rate, freight_amount):
    """
    ``lines`` is a list of ``(quantity, unit_price)``. Returns the figures
    to store on the purchase and the total of each line, in order.

        subtotal  = sum of line totals
        taxable   = subtotal - discount
        vat       = taxable x vat_rate / 100
        net total = taxable + vat + freight   (freight is not taxed)
    """
    line_totals = [money(quantity * price) for quantity, price in lines]

    subtotal = sum(line_totals, ZERO)
    discount_amount = money(discount_amount)
    freight_amount = money(freight_amount)
    vat_rate = Decimal(vat_rate).quantize(CENT, rounding=ROUND_HALF_UP)

    if discount_amount > subtotal:
        raise serializers.ValidationError(
            {"discount_amount": "The discount cannot be more than the subtotal."}
        )

    taxable = subtotal - discount_amount
    vat_amount = money(taxable * vat_rate / HUNDRED)

    return {
        "subtotal": subtotal,
        "discount_amount": discount_amount,
        "vat_rate": vat_rate,
        "vat_amount": vat_amount,
        "freight_amount": freight_amount,
        "net_total": taxable + vat_amount + freight_amount,
        "line_totals": line_totals,
    }


def paid_total(purchase):
    return (
        purchase.payments.filter(status=PurchasePayment.Status.ACTIVE)
        .aggregate(total=Sum("amount"))["total"]
        or ZERO
    )


def payment_status(net_total, paid):
    """``unpaid``, ``partial`` or ``paid``."""
    if paid >= net_total:
        return "paid"

    return "partial" if paid > 0 else "unpaid"


# ---------------------------------------------------------------------------
# Create, edit, cancel
# ---------------------------------------------------------------------------

def _next_number(business):
    # Locking the business row makes two simultaneous purchases take
    # different numbers.
    Business.objects.select_for_update().get(pk=business.pk)

    last = Purchase.objects.filter(business=business).aggregate(
        last=Max("number")
    )["last"]

    return (last or 0) + 1


def _write_items(purchase, items, totals):
    PurchaseItem.objects.bulk_create([
        PurchaseItem(
            purchase=purchase,
            product=item["product"],
            quantity=item["quantity"],
            unit_price=money(item["unit_price"]),
            line_total=line_total,
        )
        for item, line_total in zip(items, totals["line_totals"])
    ])


def _header_fields(totals):
    return {k: v for k, v in totals.items() if k != "line_totals"}


@transaction.atomic
def create_purchase(*, business, user, items, **header):
    totals = calculate_totals(
        [(i["quantity"], i["unit_price"]) for i in items],
        header.get("discount_amount", ZERO),
        header.get("vat_rate", ZERO),
        header.get("freight_amount", ZERO),
    )

    header = {
        k: v
        for k, v in header.items()
        if k not in ("discount_amount", "vat_rate", "freight_amount")
    }

    purchase = Purchase.objects.create(
        business=business,
        number=_next_number(business),
        created_by=user,
        **header,
        **_header_fields(totals),
    )

    _write_items(purchase, items, totals)

    return purchase


def _lock_draft(purchase):
    purchase = Purchase.objects.select_for_update().get(pk=purchase.pk)

    if purchase.status != Purchase.Status.DRAFT:
        raise Conflict(
            f"Only a draft purchase can be changed; this one is "
            f"{purchase.status}.",
            code="document_not_editable",
        )

    return purchase


@transaction.atomic
def update_purchase(*, purchase, items=None, **header):
    """
    Change a draft. When ``items`` is given it replaces all lines (safe,
    because a draft has no stock, returns or history attached to its lines).
    """
    purchase = _lock_draft(purchase)

    for field, value in header.items():
        setattr(purchase, field, value)

    if items is None:
        items_data = [
            {"quantity": i.quantity, "unit_price": i.unit_price}
            for i in purchase.items.all()
        ]
    else:
        items_data = items

    totals = calculate_totals(
        [(i["quantity"], i["unit_price"]) for i in items_data],
        purchase.discount_amount,
        purchase.vat_rate,
        purchase.freight_amount,
    )

    paid = paid_total(purchase)

    if totals["net_total"] < paid:
        raise serializers.ValidationError({
            "items": (
                f"The new total is less than the {paid} already paid. "
                "Void a payment first."
            )
        })

    for field, value in _header_fields(totals).items():
        setattr(purchase, field, value)

    purchase.save()

    if items is not None:
        purchase.items.all().delete()
        _write_items(purchase, items, totals)

    return purchase


@transaction.atomic
def cancel_purchase(*, purchase, user, reason):
    purchase = _lock_draft(purchase)

    if purchase.payments.filter(status=PurchasePayment.Status.ACTIVE).exists():
        raise Conflict(
            "Void the payments of this purchase before cancelling it.",
            code="payments_exist",
        )

    purchase.status = Purchase.Status.CANCELLED
    purchase.cancelled_by = user
    purchase.cancelled_at = timezone.now()
    purchase.cancel_reason = reason
    purchase.save()

    return purchase


# ---------------------------------------------------------------------------
# Receiving stock
# ---------------------------------------------------------------------------

@transaction.atomic
def receive_purchase(*, purchase, user):
    """
    The goods have arrived: add every line to the business's stock and lock
    the purchase. Stock moves exactly once; a second call is refused, and if
    any line cannot be received nothing is changed.
    """
    purchase = Purchase.objects.select_for_update().select_related(
        "business", "supplier"
    ).get(pk=purchase.pk)

    if purchase.status != Purchase.Status.DRAFT:
        raise Conflict(
            f"Only a draft purchase can be received; this one is "
            f"{purchase.status}.",
            code="invalid_status",
        )

    for item in purchase.items.select_related("product"):
        create_stock_movement(
            user=user,
            business=purchase.business,
            product=item.product,
            movement_type=StockMovement.MovementType.PURCHASE_RECEIPT,
            quantity=item.quantity,
            reference=purchase.purchase_no,
            notes=f"Purchase from {purchase.supplier.name}",
        )

    purchase.status = Purchase.Status.RECEIVED
    purchase.received_by = user
    purchase.received_at = timezone.now()
    purchase.save()

    return purchase


# ---------------------------------------------------------------------------
# Supplier payments
# ---------------------------------------------------------------------------

@transaction.atomic
def create_payment(*, purchase, user, amount, paid_on, method,
                   reference="", notes=""):
    from apps.finance.services import record_purchase_expense

    purchase = Purchase.objects.select_for_update().get(pk=purchase.pk)

    if purchase.status == Purchase.Status.CANCELLED:
        raise Conflict(
            "A cancelled purchase cannot be paid.", code="invalid_status"
        )

    paid = paid_total(purchase)
    amount = money(amount)

    if paid + amount > purchase.net_total:
        raise serializers.ValidationError({
            "amount": (
                f"This is more than is still owed "
                f"({purchase.net_total - paid})."
            )
        })

    payment = PurchasePayment.objects.create(
        purchase=purchase,
        amount=amount,
        paid_on=paid_on,
        method=method,
        reference=reference,
        notes=notes,
        paid_by=user,
    )

    record_purchase_expense(payment)

    return payment


@transaction.atomic
def void_payment(*, payment, user, reason):
    from apps.finance.services import void_purchase_expense

    payment = PurchasePayment.objects.select_for_update().get(pk=payment.pk)

    if payment.status != PurchasePayment.Status.ACTIVE:
        raise Conflict("This payment is already void.", code="invalid_status")

    payment.status = PurchasePayment.Status.VOID
    payment.voided_by = user
    payment.voided_at = timezone.now()
    payment.void_reason = reason
    payment.save(update_fields=["status", "voided_by", "voided_at", "void_reason"])

    void_purchase_expense(payment, user, reason)

    return payment
