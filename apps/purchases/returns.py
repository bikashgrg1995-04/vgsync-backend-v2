"""
Returning goods to a supplier, and using the credit that creates.

Rules
-----
* Only goods from a **received** purchase can be returned, only up to the
  quantity not yet returned, and every line must belong to that purchase.
* A return takes the goods out of stock (a ``purchase_return`` movement per
  line) and creates a **credit** with the supplier: the value of the goods
  with their share of the discount and VAT. Freight is not credited.
* Credit is separate from what the business owes. It is used up by a
  ``refund`` (the supplier pays money back), an ``adjustment`` (applied to
  a purchase, so that purchase owes less) or a ``replacement`` (the supplier
  sends goods instead: the credit is used up and the goods are *stock to
  receive* until they arrive). Credit cannot go below zero.
* Returns and settlements are never edited or deleted; they are voided.
"""

from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework import serializers

from apps.common.exceptions import Conflict
from apps.inventory.models import StockMovement
from apps.inventory.services.stock import create_stock_movement

from .models import (
    CreditSettlement,
    CreditSettlementItem,
    Purchase,
    PurchaseItem,
    PurchaseReturn,
    PurchaseReturnItem,
    Supplier,
    ZERO,
)
from .services import HUNDRED, money, next_number, settled_total


# ---------------------------------------------------------------------------
# Credit
# ---------------------------------------------------------------------------

def credit_balance(supplier):
    """Credit the business has with the supplier right now."""
    earned = (
        PurchaseReturn.objects.filter(
            supplier=supplier, status=PurchaseReturn.Status.ACTIVE
        ).aggregate(total=Sum("credit_amount"))["total"]
        or ZERO
    )
    used = (
        CreditSettlement.objects.filter(
            supplier=supplier, status=CreditSettlement.Status.ACTIVE
        ).aggregate(total=Sum("amount"))["total"]
        or ZERO
    )

    return earned - used


def line_credit(purchase, item, quantity):
    """
    Value of returning ``quantity`` of a line: the line amount less its share
    of the purchase discount, plus VAT on what is left.
    """
    value = money(quantity * item.unit_price)

    share = ZERO

    if purchase.subtotal > 0:
        share = money(purchase.discount_amount * value / purchase.subtotal)

    taxable = value - share

    return taxable + money(taxable * purchase.vat_rate / HUNDRED)


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------

@transaction.atomic
def create_return(*, purchase, user, return_date, reason, notes="", items):
    """
    ``items``: ``[{"purchase_item", "quantity", "stock_condition"}]``.
    Everything happens together: if any line cannot be taken out of stock,
    nothing is returned.
    """
    # Locking the purchase makes two returns of the same purchase wait for
    # each other, so the returnable quantity is always checked fresh.
    purchase = (
        Purchase.objects.select_for_update()
        .select_related("business", "supplier")
        .get(pk=purchase.pk)
    )

    if purchase.status not in Purchase.BILLED:
        raise Conflict(
            "Only goods from a received purchase can be returned; this "
            f"purchase is {purchase.status}.",
            code="invalid_status",
        )

    seen = set()
    lines = []

    for entry in items:
        item = entry["purchase_item"]

        if item.purchase_id != purchase.pk:
            raise serializers.ValidationError(
                {"items": "A line does not belong to this purchase."}
            )

        if item.pk in seen:
            raise serializers.ValidationError(
                {"items": "A line is listed twice. Combine the quantities."}
            )

        seen.add(item.pk)
        lines.append((item, entry["quantity"], entry["stock_condition"]))

    returned = {
        row["purchase_item"]: row["total"]
        for row in PurchaseReturnItem.objects.filter(
            purchase_item__purchase=purchase,
            purchase_return__status=PurchaseReturn.Status.ACTIVE,
        )
        .values("purchase_item")
        .annotate(total=Sum("quantity"))
    }

    creditable = purchase.net_total - purchase.freight_amount
    credited = (
        PurchaseReturn.objects.filter(
            purchase=purchase, status=PurchaseReturn.Status.ACTIVE
        ).aggregate(total=Sum("credit_amount"))["total"]
        or ZERO
    )

    priced = []

    for item, quantity, condition in lines:
        # Only goods that actually arrived can go back.
        left = item.received_quantity - returned.get(item.pk, Decimal("0"))

        if quantity > left:
            raise serializers.ValidationError({
                "items": (
                    f"{item.product.name}: only {left} left to return "
                    f"(received {item.received_quantity})."
                )
            })

        # Rounding can never make the credits add up to more than the
        # purchase's goods were worth.
        credit = min(
            line_credit(purchase, item, quantity),
            max(creditable - credited, ZERO),
        )
        credited += credit

        priced.append((item, quantity, condition, credit))

    ret = PurchaseReturn.objects.create(
        business=purchase.business,
        number=next_number(purchase.business, PurchaseReturn),
        purchase=purchase,
        supplier=purchase.supplier,
        return_date=return_date,
        reason=reason,
        notes=notes,
        credit_amount=sum((p[3] for p in priced), ZERO),
        created_by=user,
    )

    for item, quantity, condition, credit in priced:
        PurchaseReturnItem.objects.create(
            purchase_return=ret,
            purchase_item=item,
            product=item.product,
            quantity=quantity,
            stock_condition=condition,
            credit_amount=credit,
        )

        # Raises if the stock is no longer there (for example already sold),
        # which undoes the whole return.
        create_stock_movement(
            user=user,
            business=purchase.business,
            product=item.product,
            movement_type=StockMovement.MovementType.PURCHASE_RETURN,
            quantity=quantity,
            stock_condition=condition,
            reference=ret.return_no,
            notes=f"Returned to {purchase.supplier.name} ({purchase.purchase_no})",
        )

    return ret


@transaction.atomic
def void_return(*, purchase_return, user, reason):
    """
    Undo a return: the goods go back into stock and the credit is removed.
    Refused when the credit has already been used.
    """
    supplier = Supplier.objects.select_for_update().get(
        pk=purchase_return.supplier_id
    )
    ret = (
        PurchaseReturn.objects.select_for_update()
        .select_related("business")
        .get(pk=purchase_return.pk)
    )

    if ret.status != PurchaseReturn.Status.ACTIVE:
        raise Conflict("This return is already void.", code="invalid_status")

    if credit_balance(supplier) < ret.credit_amount:
        raise Conflict(
            "The credit from this return has already been used. Void the "
            "refund or applied credit first.",
            code="credit_in_use",
        )

    for item in ret.items.select_related("product"):
        create_stock_movement(
            user=user,
            business=ret.business,
            product=item.product,
            movement_type=StockMovement.MovementType.ADJUSTMENT_IN,
            quantity=item.quantity,
            stock_condition=item.stock_condition,
            reference=ret.return_no,
            notes=f"Return {ret.return_no} voided",
        )

    ret.status = PurchaseReturn.Status.VOID
    ret.voided_by = user
    ret.voided_at = timezone.now()
    ret.void_reason = reason
    ret.save(update_fields=["status", "voided_by", "voided_at", "void_reason"])

    return ret


# ---------------------------------------------------------------------------
# Using the credit
# ---------------------------------------------------------------------------

@transaction.atomic
def create_settlement(*, supplier, user, kind, amount, settled_on,
                      purchase=None, method="", reference="", notes="",
                      items=()):
    supplier = Supplier.objects.select_for_update().get(pk=supplier.pk)
    amount = money(amount)

    available = credit_balance(supplier)

    if amount > available:
        raise serializers.ValidationError({
            "amount": f"More than the credit available ({available})."
        })

    if kind == CreditSettlement.Kind.ADJUSTMENT:
        purchase = Purchase.objects.select_for_update().get(pk=purchase.pk)

        if purchase.supplier_id != supplier.pk:
            raise serializers.ValidationError(
                {"purchase": "This purchase is from another supplier."}
            )

        if purchase.status == Purchase.Status.CANCELLED:
            raise Conflict(
                "A cancelled purchase cannot take credit.", code="invalid_status"
            )

        owed = purchase.net_total - settled_total(purchase)

        if amount > owed:
            raise serializers.ValidationError({
                "amount": f"More than this purchase still owes ({owed})."
            })

    else:
        purchase = None

    if kind == CreditSettlement.Kind.REPLACEMENT:
        products = [entry["product"].pk for entry in items]

        if not items:
            raise serializers.ValidationError(
                {"items": "List the goods the supplier will send."}
            )

        if len(set(products)) != len(products):
            raise serializers.ValidationError(
                {"items": "A product is listed twice. Combine the quantities."}
            )

        method = ""
    elif items:
        raise serializers.ValidationError(
            {"items": "Goods are listed for a replacement only."}
        )

    settlement = CreditSettlement.objects.create(
        supplier=supplier,
        kind=kind,
        amount=amount,
        settled_on=settled_on,
        purchase=purchase,
        method=method,
        reference=reference,
        notes=notes,
        created_by=user,
    )

    CreditSettlementItem.objects.bulk_create(
        CreditSettlementItem(
            settlement=settlement,
            product=entry["product"],
            quantity=entry["quantity"],
        )
        for entry in items
    )

    return settlement


@transaction.atomic
def receive_replacement(*, settlement, user, received_on):
    """
    The replacement goods arrived: add them to stock, once. Stock moves
    exactly once; if any line cannot be received nothing is changed.
    """
    settlement = CreditSettlement.objects.select_for_update().select_related(
        "supplier__business"
    ).get(pk=settlement.pk)

    if (
        settlement.kind != CreditSettlement.Kind.REPLACEMENT
        or settlement.status != CreditSettlement.Status.ACTIVE
    ):
        raise Conflict(
            "Only a replacement that is still active can be received.",
            code="invalid_status",
        )

    if settlement.received_on:
        raise Conflict("These goods were already received.", code="already_received")

    reference = f"RPL-{settlement.pk:06d}"

    for item in settlement.items.select_related("product"):
        create_stock_movement(
            user=user,
            business=settlement.supplier.business,
            product=item.product,
            movement_type=StockMovement.MovementType.PURCHASE_RECEIPT,
            quantity=item.quantity,
            reference=reference,
            notes=f"Replacement from {settlement.supplier.name}",
        )

    settlement.received_on = received_on
    settlement.received_by = user
    settlement.save(update_fields=["received_on", "received_by"])

    return settlement


@transaction.atomic
def void_settlement(*, settlement, user, reason):
    settlement = CreditSettlement.objects.select_for_update().get(
        pk=settlement.pk
    )

    if settlement.status != CreditSettlement.Status.ACTIVE:
        raise Conflict("This is already void.", code="invalid_status")

    if settlement.received_on:
        raise Conflict(
            "The replacement goods are already in stock, so this cannot be "
            "voided.",
            code="already_received",
        )

    settlement.status = CreditSettlement.Status.VOID
    settlement.voided_by = user
    settlement.voided_at = timezone.now()
    settlement.void_reason = reason
    settlement.save(
        update_fields=["status", "voided_by", "voided_at", "void_reason"]
    )

    return settlement
