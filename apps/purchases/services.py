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

from .models import (
    CreditSettlement,
    Purchase,
    PurchaseItem,
    PurchasePayment,
    PurchaseReceipt,
    PurchaseReceiptItem,
    ZERO,
)

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


def credit_applied_total(purchase):
    """Supplier credit already applied against this purchase."""
    return (
        purchase.credit_settlements.filter(
            kind=CreditSettlement.Kind.ADJUSTMENT,
            status=CreditSettlement.Status.ACTIVE,
        ).aggregate(total=Sum("amount"))["total"]
        or ZERO
    )


def settled_total(purchase):
    """Paid in money plus credit applied: what no longer has to be paid."""
    return paid_total(purchase) + credit_applied_total(purchase)


def payment_status(net_total, paid):
    """``unpaid``, ``partial`` or ``paid``."""
    if paid >= net_total:
        return "paid"

    return "partial" if paid > 0 else "unpaid"


# ---------------------------------------------------------------------------
# Create, edit, cancel
# ---------------------------------------------------------------------------

def next_number(business, model=Purchase):
    # Locking the business row makes two simultaneous documents take
    # different numbers.
    Business.objects.select_for_update().get(pk=business.pk)

    last = model.objects.filter(business=business).aggregate(
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
        number=next_number(business),
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

    settled = settled_total(purchase)

    if totals["net_total"] < settled:
        raise serializers.ValidationError({
            "items": (
                f"The new total is less than the {settled} already paid or "
                "covered by credit. Void a payment first."
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

    if (
        purchase.payments.filter(status=PurchasePayment.Status.ACTIVE).exists()
        or credit_applied_total(purchase) > 0
    ):
        raise Conflict(
            "Void the payments and applied credit of this purchase before "
            "cancelling it.",
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
def receive_purchase(*, purchase, user, items=None, received_on=None, notes=""):
    """
    Goods have arrived: add them to the business's stock.

    ``items`` is ``[{"purchase_item", "quantity"}]`` for a part delivery; with
    no ``items`` everything still to come is received. Each call is one
    delivery (a ``PurchaseReceipt``). Once every line is fully received the
    purchase is ``received``; until then it is ``partial``. If any line
    cannot be received nothing is changed.
    """
    purchase = Purchase.objects.select_for_update().select_related(
        "business", "supplier"
    ).get(pk=purchase.pk)

    if purchase.status not in (Purchase.Status.DRAFT, Purchase.Status.PARTIAL):
        raise Conflict(
            f"Only a draft or partly received purchase can be received; "
            f"this one is {purchase.status}.",
            code="invalid_status",
        )

    lines = {
        item.pk: item
        for item in purchase.items.select_for_update().select_related("product")
    }

    if items is None:
        wanted = [
            (item, item.quantity - item.received_quantity)
            for item in lines.values()
            if item.quantity > item.received_quantity
        ]
    else:
        wanted = []
        seen = set()

        for entry in items:
            item = lines.get(entry["purchase_item"].pk)

            if item is None:
                raise serializers.ValidationError(
                    {"items": "A line does not belong to this purchase."}
                )

            if item.pk in seen:
                raise serializers.ValidationError(
                    {"items": "A line is listed twice. Combine the quantities."}
                )

            seen.add(item.pk)

            left = item.quantity - item.received_quantity

            if entry["quantity"] > left:
                raise serializers.ValidationError({
                    "items": (
                        f"{item.product.name}: only {left} still to come "
                        f"(ordered {item.quantity})."
                    )
                })

            wanted.append((item, entry["quantity"]))

    if not wanted:
        raise Conflict(
            "Nothing is left to receive on this purchase.",
            code="nothing_to_receive",
        )

    receipt = PurchaseReceipt.objects.create(
        purchase=purchase,
        received_on=received_on or timezone.localdate(),
        notes=notes,
        received_by=user,
    )

    for item, quantity in wanted:
        create_stock_movement(
            user=user,
            business=purchase.business,
            product=item.product,
            movement_type=StockMovement.MovementType.PURCHASE_RECEIPT,
            quantity=quantity,
            reference=purchase.purchase_no,
            notes=f"Purchase from {purchase.supplier.name}",
        )

        PurchaseReceiptItem.objects.create(
            receipt=receipt, purchase_item=item, quantity=quantity
        )

        item.received_quantity += quantity
        item.save(update_fields=["received_quantity"])

    complete = all(
        item.received_quantity >= item.quantity for item in lines.values()
    )

    if complete:
        purchase.status = Purchase.Status.RECEIVED
        purchase.received_by = user
        purchase.received_at = timezone.now()
    else:
        purchase.status = Purchase.Status.PARTIAL

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

    settled = settled_total(purchase)
    amount = money(amount)

    if settled + amount > purchase.net_total:
        raise serializers.ValidationError({
            "amount": (
                f"This is more than is still owed "
                f"({purchase.net_total - settled})."
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
