"""
Goods coming back from customers, and the credit that creates.

Rules
-----
* Only goods from a **confirmed** sale can be returned: stock goods or the
  parts of a maintenance sale. A service charge and a bike cannot be
  returned. Every line must belong to that sale, none can be listed twice,
  and nothing beyond what has not been returned yet.
* A return puts the goods back into good stock (a ``sale_return`` movement
  per line) and is worth their value with their share of the discount and
  VAT (``goods_value``).
* That value first takes off what the customer still owes on the same sale
  (``applied_amount``). Whatever is left is **credit to pay**
  (``credit_amount``): money the business owes the customer, kept apart
  from what the customer owes. A walk-in customer is refunded straight away.
* Credit is used up by a ``refund`` (cash goes back to the customer), an
  ``adjustment`` (set against what the customer owes on another sale) or a
  ``replacement`` (other goods are given instead: the credit is used up and
  the goods are *stock to give* until they are delivered). Credit cannot go
  below zero.
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
from apps.purchases.services import HUNDRED, money, next_number

from .models import (
    Customer,
    Sale,
    SaleCreditSettlement,
    SaleCreditSettlementItem,
    SaleReturn,
    SaleReturnItem,
    ZERO,
)
from .services import settled_total, sync_followups


# ---------------------------------------------------------------------------
# Credit
# ---------------------------------------------------------------------------

def credit_balance(customer):
    """Credit the business owes the customer right now."""
    earned = (
        SaleReturn.objects.filter(
            customer=customer, status=SaleReturn.Status.ACTIVE
        ).aggregate(total=Sum("credit_amount"))["total"]
        or ZERO
    )
    used = (
        SaleCreditSettlement.objects.filter(
            customer=customer, status=SaleCreditSettlement.Status.ACTIVE
        ).aggregate(total=Sum("amount"))["total"]
        or ZERO
    )

    return earned - used


def line_credit(sale, item, quantity):
    """
    Value of returning ``quantity`` of a line: the line amount less its share
    of the sale discount, plus VAT on what is left.
    """
    value = money(quantity * item.unit_price)

    share = ZERO

    if sale.subtotal > 0:
        share = money(sale.discount_amount * value / sale.subtotal)

    taxable = value - share

    return taxable + money(taxable * sale.vat_rate / HUNDRED)


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------

@transaction.atomic
def create_return(*, sale, user, return_date, reason, notes="", items,
                  refund_method="", reference=""):
    """
    ``items``: ``[{"sale_item", "quantity"}]``.
    Everything happens together: if anything cannot be done, nothing is
    returned.
    """
    # Locking the sale makes two returns of the same sale wait for each
    # other, so the returnable quantity is always checked fresh.
    sale = (
        Sale.objects.select_for_update(of=("self",))
        .select_related("business")
        .get(pk=sale.pk)
    )

    if sale.status != Sale.Status.CONFIRMED:
        raise Conflict(
            "Only goods from a confirmed sale can be returned; this sale is "
            f"{sale.status}.",
            code="invalid_status",
        )

    customer = None

    if sale.customer_id:
        customer = Customer.objects.select_for_update().get(pk=sale.customer_id)

    walk_in = customer is None

    if walk_in and not refund_method:
        raise serializers.ValidationError({
            "refund_method": (
                "A walk-in customer is refunded straight away. Say how "
                "the money is paid back."
            )
        })

    if not walk_in and refund_method:
        raise serializers.ValidationError({
            "refund_method": (
                "Only a walk-in return is refunded with the return. Use "
                "the credit of the customer to refund later."
            )
        })

    seen = set()
    lines = []

    for entry in items:
        item = entry["sale_item"]

        if item.sale_id != sale.pk:
            raise serializers.ValidationError(
                {"items": "A line does not belong to this sale."}
            )

        if item.product_id is None:
            raise serializers.ValidationError(
                {"items": f"{item.description}: a service charge cannot be returned."}
            )

        if item.bike_unit_id:
            raise serializers.ValidationError(
                {"items": "A bike cannot be returned."}
            )

        if item.pk in seen:
            raise serializers.ValidationError(
                {"items": "A line is listed twice. Combine the quantities."}
            )

        seen.add(item.pk)
        lines.append((item, entry["quantity"]))

    returned = {
        row["sale_item"]: row["total"]
        for row in SaleReturnItem.objects.filter(
            sale_item__sale=sale,
            sale_return__status=SaleReturn.Status.ACTIVE,
        )
        .values("sale_item")
        .annotate(total=Sum("quantity"))
    }

    creditable = sale.net_total
    credited = (
        SaleReturnItem.objects.filter(
            sale_item__sale=sale,
            sale_return__status=SaleReturn.Status.ACTIVE,
        ).aggregate(total=Sum("credit_amount"))["total"]
        or ZERO
    )

    priced = []

    for item, quantity in lines:
        left = item.quantity - returned.get(item.pk, Decimal("0"))

        if quantity > left:
            raise serializers.ValidationError({
                "items": (
                    f"{item.product.name}: only {left} left to return "
                    f"(sold {item.quantity})."
                )
            })

        # Rounding can never make the values add up to more than the sale
        # was worth.
        value = min(
            line_credit(sale, item, quantity),
            max(creditable - credited, ZERO),
        )
        credited += value

        priced.append((item, quantity, value))

    goods_value = sum((p[2] for p in priced), ZERO)
    settled = settled_total(sale)
    unpaid = max(sale.amount_due - settled, ZERO)

    applied = min(goods_value, unpaid)
    credit = goods_value - applied

    ret = SaleReturn.objects.create(
        business=sale.business,
        number=next_number(sale.business, SaleReturn),
        sale=sale,
        customer=customer,
        return_date=return_date,
        reason=reason,
        notes=notes,
        applied_amount=applied,
        credit_amount=credit,
        created_by=user,
    )

    for item, quantity, value in priced:
        SaleReturnItem.objects.create(
            sale_return=ret,
            sale_item=item,
            product=item.product,
            quantity=quantity,
            credit_amount=value,
        )

        create_stock_movement(
            user=user,
            business=sale.business,
            product=item.product,
            movement_type=StockMovement.MovementType.SALE_RETURN,
            quantity=quantity,
            reference=ret.return_no,
            notes=f"Returned on {sale.invoice_no} ({sale.sale_no})",
        )

    if walk_in and credit > 0:
        SaleCreditSettlement.objects.create(
            business=sale.business,
            customer=None,
            sale_return=ret,
            kind=SaleCreditSettlement.Kind.REFUND,
            amount=credit,
            settled_on=return_date,
            method=refund_method,
            reference=reference,
            created_by=user,
        )

    sync_followups(sale)

    return ret


@transaction.atomic
def void_return(*, sale_return, user, reason):
    """
    Undo a return: the goods go out of stock again and what it took off the
    sale (or gave as credit) is removed. Refused when its credit has already
    been used. A walk-in refund made with the return is voided with it.
    """
    ret = (
        SaleReturn.objects.select_for_update(of=("self",))
        .select_related("business", "sale")
        .get(pk=sale_return.pk)
    )

    if ret.customer_id:
        Customer.objects.select_for_update().get(pk=ret.customer_id)

    if ret.status != SaleReturn.Status.ACTIVE:
        raise Conflict("This return is already void.", code="invalid_status")

    if ret.customer_id and credit_balance(ret.customer) < ret.credit_amount:
        raise Conflict(
            "The credit from this return has already been used. Void the "
            "refund, applied credit or replacement first.",
            code="credit_in_use",
        )

    for item in ret.items.select_related("product"):
        # Raises if the goods are no longer in stock, which undoes it all.
        create_stock_movement(
            user=user,
            business=ret.business,
            product=item.product,
            movement_type=StockMovement.MovementType.ADJUSTMENT_OUT,
            quantity=item.quantity,
            reference=ret.return_no,
            notes=f"Return {ret.return_no} voided",
        )

    now = timezone.now()

    for refund in ret.settlements.filter(
        status=SaleCreditSettlement.Status.ACTIVE
    ):
        refund.status = SaleCreditSettlement.Status.VOID
        refund.voided_by = user
        refund.voided_at = now
        refund.void_reason = f"Return {ret.return_no} voided: {reason}"
        refund.save(
            update_fields=["status", "voided_by", "voided_at", "void_reason"]
        )

    ret.status = SaleReturn.Status.VOID
    ret.voided_by = user
    ret.voided_at = now
    ret.void_reason = reason
    ret.save(update_fields=["status", "voided_by", "voided_at", "void_reason"])

    sync_followups(ret.sale)

    return ret


# ---------------------------------------------------------------------------
# Using the credit
# ---------------------------------------------------------------------------

@transaction.atomic
def create_settlement(*, business, customer, user, kind, amount, settled_on,
                      sale=None, method="", reference="", notes="", items=()):
    customer = Customer.objects.select_for_update().get(pk=customer.pk)
    amount = money(amount)

    available = credit_balance(customer)

    if amount > available:
        raise serializers.ValidationError({
            "amount": f"More than the credit available ({available})."
        })

    if kind == SaleCreditSettlement.Kind.ADJUSTMENT:
        sale = Sale.objects.select_for_update(of=("self",)).get(pk=sale.pk)

        if sale.customer_id != customer.pk:
            raise serializers.ValidationError(
                {"sale": "This sale belongs to another customer."}
            )

        if sale.status == Sale.Status.CANCELLED:
            raise Conflict(
                "A cancelled sale cannot take credit.", code="invalid_status"
            )

        if sale.payment_mode == Sale.PaymentMode.EMI:
            raise serializers.ValidationError(
                {"sale": "Credit cannot be set against an EMI sale."}
            )

        owed = sale.amount_due - settled_total(sale)

        if amount > owed:
            raise serializers.ValidationError({
                "amount": f"More than this sale still owes ({owed})."
            })

    else:
        sale = None

    if kind == SaleCreditSettlement.Kind.REPLACEMENT:
        products = [entry["product"].pk for entry in items]

        if not items:
            raise serializers.ValidationError(
                {"items": "List the goods that will be given."}
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

    settlement = SaleCreditSettlement.objects.create(
        business=business,
        customer=customer,
        kind=kind,
        amount=amount,
        settled_on=settled_on,
        sale=sale,
        method=method,
        reference=reference,
        notes=notes,
        created_by=user,
    )

    SaleCreditSettlementItem.objects.bulk_create(
        SaleCreditSettlementItem(
            settlement=settlement,
            product=entry["product"],
            quantity=entry["quantity"],
        )
        for entry in items
    )

    if sale is not None:
        sync_followups(sale)

    return settlement


@transaction.atomic
def deliver_replacement(*, settlement, user, delivered_on):
    """
    The replacement goods are handed over: take them out of stock, once.
    Stock moves exactly once; if any line cannot be issued nothing changes.
    """
    settlement = (
        SaleCreditSettlement.objects.select_for_update(of=("self",))
        .select_related("business", "customer")
        .get(pk=settlement.pk)
    )

    if (
        settlement.kind != SaleCreditSettlement.Kind.REPLACEMENT
        or settlement.status != SaleCreditSettlement.Status.ACTIVE
    ):
        raise Conflict(
            "Only a replacement that is still active can be delivered.",
            code="invalid_status",
        )

    if settlement.delivered_on:
        raise Conflict("These goods were already delivered.", code="already_delivered")

    reference = f"RPL-{settlement.pk:06d}"

    for item in settlement.items.select_related("product"):
        create_stock_movement(
            user=user,
            business=settlement.business,
            product=item.product,
            movement_type=StockMovement.MovementType.SALE_ISSUE,
            quantity=item.quantity,
            reference=reference,
            notes=f"Replacement for {settlement.customer.name}",
        )

    settlement.delivered_on = delivered_on
    settlement.delivered_by = user
    settlement.save(update_fields=["delivered_on", "delivered_by"])

    return settlement


@transaction.atomic
def void_settlement(*, settlement, user, reason):
    settlement = (
        SaleCreditSettlement.objects.select_for_update()
        .get(pk=settlement.pk)
    )

    if settlement.status != SaleCreditSettlement.Status.ACTIVE:
        raise Conflict("This is already void.", code="invalid_status")

    if settlement.sale_return_id:
        raise Conflict(
            "This refund was made with a walk-in return. Void the return "
            "instead.",
            code="return_refund_locked",
        )

    if settlement.delivered_on:
        raise Conflict(
            "The replacement goods were already delivered, so this cannot "
            "be voided.",
            code="already_delivered",
        )

    settlement.status = SaleCreditSettlement.Status.VOID
    settlement.voided_by = user
    settlement.voided_at = timezone.now()
    settlement.void_reason = reason
    settlement.save(
        update_fields=["status", "voided_by", "voided_at", "void_reason"]
    )

    if settlement.sale_id:
        sync_followups(settlement.sale)

    return settlement
