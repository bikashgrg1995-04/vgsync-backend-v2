"""
Sales rules in one place: totals, numbering, issuing stock, payments.

Every total on a sale comes from ``calculate_totals`` and is recalculated
whenever the lines change; the client never sends a total.
"""

import calendar
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone
from rest_framework import serializers

from apps.businesses.models import Business
from apps.common.exceptions import Conflict
from apps.inventory.models import StockBalance, StockMovement
from apps.inventory.services.stock import create_stock_movement
from apps.purchases.services import (
    calculate_totals as _calculate_totals,
    money,
    next_number,
    payment_status,
)

from .models import (
    ZERO,
    BikeUnit,
    EmiInstallment,
    Followup,
    Sale,
    SaleCreditSettlement,
    SaleItem,
    SalePayment,
)

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


MAINTENANCE_POST_DAYS = 3
MAINTENANCE_FOLLOWUP_DAYS = 90
MAINTENANCE_KINDS = (
    Followup.Kind.MAINTENANCE_POST,
    Followup.Kind.MAINTENANCE_DUE,
)
CREDIT_FOLLOWUP_DAYS = 30


def add_months(day, months):
    """The same day ``months`` later; a short month uses its last day."""
    index = day.month - 1 + months
    year = day.year + index // 12
    month = index % 12 + 1

    return day.replace(
        year=year, month=month,
        day=min(day.day, calendar.monthrange(year, month)[1]),
    )


def _price(price, product):
    if price is None and product is not None:
        price = product.sale_price

    return money(price) if price is not None else None


def _resolve_items(business, sale_type, items):
    """
    Turn the lines sent by the client into lines ready to store, checking
    them against the kind of sale:

    * stock: products only.
    * bike: bike units only, one each, in stock in this business.
    * maintenance: stock parts, and service charges (a description, no
      product, nothing taken from stock).
    """
    resolved = []
    seen_units = set()

    for entry in items:
        product = entry.get("product")
        unit = entry.get("bike_unit")
        description = (entry.get("description") or "").strip()
        quantity = entry.get("quantity")

        if sale_type == Sale.SaleType.BIKE:
            if unit is None or product is not None or description:
                raise serializers.ValidationError({
                    "items": "A bike sale lists bike units (bike_unit) only."
                })

            if quantity not in (None, 1) and Decimal(quantity) != 1:
                raise serializers.ValidationError(
                    {"items": "A bike unit is sold one at a time."}
                )

            if unit.business_id != business.pk:
                raise serializers.ValidationError(
                    {"items": "Choose a bike unit of this business."}
                )

            if unit.status != BikeUnit.Status.IN_STOCK:
                raise serializers.ValidationError({
                    "items": f"Bike {unit.chassis_no} is not in stock."
                })

            if unit.pk in seen_units:
                raise serializers.ValidationError({
                    "items": f"Bike {unit.chassis_no} is listed twice."
                })

            seen_units.add(unit.pk)
            product = unit.product
            quantity = Decimal("1")

        else:
            if unit is not None:
                raise serializers.ValidationError(
                    {"items": "Bike units are sold in a bike sale."}
                )

            if quantity is None:
                raise serializers.ValidationError(
                    {"items": "Every line needs a quantity."}
                )

            if product is None and sale_type != Sale.SaleType.MAINTENANCE:
                raise serializers.ValidationError(
                    {"items": "Choose a product for every line."}
                )

            if product is None and not description:
                raise serializers.ValidationError({
                    "items": "A service charge needs a description."
                })

            if product is not None and description:
                raise serializers.ValidationError({
                    "items": "A line is a product or a service charge, not both."
                })

        price = _price(entry.get("unit_price"), product)

        if price is None or price <= 0:
            what = product.name if product else (description or "This line")
            raise serializers.ValidationError({
                "items": f"{what} has no price. Enter a price for this line."
            })

        resolved.append({
            "product": product,
            "bike_unit": unit,
            "description": description,
            "quantity": quantity,
            "unit_price": price,
        })

    return resolved


def _check_terms(sale, net_total):
    """
    Settle the payment terms of the sale (full payment or EMI) against the
    net total, and return the finance charge. ``sale`` carries the values
    being saved.
    """
    emi = sale.payment_mode == Sale.PaymentMode.EMI

    if sale.sale_type != Sale.SaleType.MAINTENANCE and (
        sale.vehicle_chassis_no or sale.vehicle_reg_no
    ):
        raise serializers.ValidationError(
            {"vehicle_chassis_no": "The customer's bike is for maintenance sales."}
        )

    if not emi:
        if (
            sale.down_payment
            or sale.emi_months
            or sale.emi_amount
            or sale.first_emi_due
        ):
            raise serializers.ValidationError({
                "payment_mode": "Down payment and EMI details need payment_mode emi."
            })

        return ZERO

    if sale.sale_type != Sale.SaleType.BIKE:
        raise serializers.ValidationError(
            {"payment_mode": "EMI is for bike sales."}
        )

    if sale.customer_id is None:
        raise serializers.ValidationError(
            {"customer": "An EMI sale needs a customer."}
        )

    if not sale.emi_months or not sale.emi_amount:
        raise serializers.ValidationError({
            "emi_months": "Give the number of months and the monthly EMI amount."
        })

    sale.down_payment = money(sale.down_payment or 0)
    sale.emi_amount = money(sale.emi_amount)

    if sale.down_payment > net_total:
        raise serializers.ValidationError(
            {"down_payment": "The down payment cannot be more than the total."}
        )

    payable = sale.down_payment + sale.emi_amount * sale.emi_months

    if payable < net_total:
        raise serializers.ValidationError({
            "emi_amount": (
                f"The down payment and EMIs add up to {payable}, which is "
                f"less than the total {net_total}."
            )
        })

    if not sale.first_emi_due:
        sale.first_emi_due = add_months(sale.sale_date, 1)

    # Whatever the instalments add up to above the price is the finance charge.
    return payable - net_total


def paid_total(sale):
    return (
        sale.payments.filter(status=SalePayment.Status.ACTIVE)
        .aggregate(total=Sum("amount"))["total"]
        or ZERO
    )


def credit_applied_total(sale):
    """Customer credit already set against this sale."""
    return (
        sale.credit_settlements.filter(
            kind=SaleCreditSettlement.Kind.ADJUSTMENT,
            status=SaleCreditSettlement.Status.ACTIVE,
        ).aggregate(total=Sum("amount"))["total"]
        or ZERO
    )


def returns_applied_total(sale):
    """What returned goods took off what the customer owed on this sale."""
    return (
        sale.returns.filter(status="active").aggregate(
            total=Sum("applied_amount")
        )["total"]
        or ZERO
    )


def settled_total(sale):
    """
    Received in money, plus credit applied, plus what returned goods took
    off: everything that no longer has to be paid on the sale.
    """
    return (
        paid_total(sale)
        + credit_applied_total(sale)
        + returns_applied_total(sale)
    )



# ---------------------------------------------------------------------------
# Create, edit, cancel
# ---------------------------------------------------------------------------

def _write_items(sale, items, totals):
    SaleItem.objects.bulk_create([
        SaleItem(
            sale=sale,
            product=item["product"],
            bike_unit=item["bike_unit"],
            description=item["description"],
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
    discount = header.pop("discount_amount", ZERO)
    vat_rate = header.pop("vat_rate", ZERO)

    sale = Sale(
        business=business,
        created_by=user,
        sale_type=header.pop("sale_type", Sale.SaleType.STOCK),
        **header,
    )

    items = _resolve_items(business, sale.sale_type, items)

    totals = calculate_totals(
        [(i["quantity"], i["unit_price"]) for i in items], discount, vat_rate
    )
    sale.finance_charge = _check_terms(sale, totals["net_total"])

    for field, value in _header_fields(totals).items():
        setattr(sale, field, value)

    sale.number = next_number(business, Sale)
    sale.save()

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
    because a draft has no stock or history attached to its lines). The kind
    of sale cannot be changed.
    """
    sale = _lock_draft(sale)

    if header.pop("sale_type", sale.sale_type) != sale.sale_type:
        raise serializers.ValidationError(
            {"sale_type": "The kind of sale cannot be changed. Make a new sale."}
        )

    for field, value in header.items():
        setattr(sale, field, value)

    if items is None:
        lines = [(i.quantity, i.unit_price) for i in sale.items.all()]
    else:
        items = _resolve_items(sale.business, sale.sale_type, items)
        lines = [(i["quantity"], i["unit_price"]) for i in items]

    totals = calculate_totals(lines, sale.discount_amount, sale.vat_rate)

    for field, value in _header_fields(totals).items():
        setattr(sale, field, value)

    sale.finance_charge = _check_terms(sale, totals["net_total"])
    paid = settled_total(sale)

    if sale.amount_due < paid:
        raise serializers.ValidationError({
            "items": (
                f"The new total is less than the {paid} already received or "
                "covered by credit. Void a payment first."
            )
        })

    sale.save()

    if items is not None:
        sale.items.all().delete()
        _write_items(sale, items, totals)

    return sale


@transaction.atomic
def cancel_sale(*, sale, user, reason):
    sale = _lock_draft(sale)

    if (
        sale.payments.filter(status=SalePayment.Status.ACTIVE).exists()
        or credit_applied_total(sale) > 0
    ):
        raise Conflict(
            "Void the payments and applied credit of this sale before "
            "cancelling it.",
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
    Hand over the goods: take every stock line out of the business's stock,
    mark sold bikes, give the sale its invoice number and lock it. Stock
    moves exactly once; a second call is refused, and if anything cannot be
    issued (for example not enough stock) nothing is changed.

    Confirming also sets up what comes after: the EMI instalments of a bike
    sale, two follow-ups after maintenance (3 days and 90 days after the
    service date), and a follow-up for any
    credit that is left to receive.
    """
    sale = Sale.objects.select_for_update(of=("self",)).select_related(
        "business"
    ).get(pk=sale.pk)

    if sale.status != Sale.Status.DRAFT:
        raise Conflict(
            f"Only a draft sale can be confirmed; this one is {sale.status}.",
            code="invalid_status",
        )

    items = list(sale.items.select_related("product", "bike_unit"))

    if not items:
        raise serializers.ValidationError({"items": "The sale has no lines."})

    paid = paid_total(sale)
    emi = sale.payment_mode == Sale.PaymentMode.EMI

    # Without a customer there is nobody to collect the rest from.
    if sale.customer_id is None and paid < sale.amount_due:
        raise Conflict(
            "A walk-in sale must be paid in full. Choose a customer to sell "
            "on credit.",
            code="customer_required",
        )

    if emi and paid < sale.down_payment:
        raise Conflict(
            f"Take the down payment ({sale.down_payment}) before confirming. "
            f"{paid} has been received.",
            code="down_payment_due",
        )

    invoice_number = next_invoice_number(sale.business)
    reference = f"INV-{invoice_number:06d}"

    # Lock the bikes so the same unit cannot be sold twice at once.
    units = {
        unit.pk: unit
        for unit in BikeUnit.objects.select_for_update().filter(
            pk__in=[i.bike_unit_id for i in items if i.bike_unit_id]
        )
    }

    for item in items:
        if item.product_id is None:
            continue

        if item.bike_unit_id:
            unit = units[item.bike_unit_id]

            if unit.status != BikeUnit.Status.IN_STOCK:
                raise Conflict(
                    f"Bike {unit.chassis_no} has already been sold.",
                    code="bike_unavailable",
                )

            unit.status = BikeUnit.Status.SOLD
            unit.save(update_fields=["status", "updated_at"])

        create_stock_movement(
            user=user,
            business=sale.business,
            product=item.product,
            movement_type=StockMovement.MovementType.SALE_ISSUE,
            quantity=item.quantity,
            reference=reference,
            notes=(
                f"Sale {sale.sale_no}"
                + (f", chassis {item.bike_unit.chassis_no}" if item.bike_unit_id else "")
            ),
        )

    sale.status = Sale.Status.CONFIRMED
    sale.invoice_number = invoice_number
    sale.confirmed_by = user
    sale.confirmed_at = timezone.now()
    sale.save()

    _create_followups(sale)
    sync_followups(sale)

    return sale


# ---------------------------------------------------------------------------
# EMI instalments and follow-ups
# ---------------------------------------------------------------------------

def _create_followups(sale):
    if sale.payment_mode == Sale.PaymentMode.EMI:
        for number in range(1, sale.emi_months + 1):
            installment = EmiInstallment.objects.create(
                sale=sale,
                number=number,
                due_date=add_months(sale.first_emi_due, number - 1),
                amount=sale.emi_amount,
            )

            Followup.objects.create(
                business=sale.business,
                sale=sale,
                customer=sale.customer,
                installment=installment,
                kind=Followup.Kind.EMI_DUE,
                due_date=installment.due_date,
                amount=installment.amount,
            )

    elif sale.amount_due > settled_total(sale):
        Followup.objects.create(
            business=sale.business,
            sale=sale,
            customer=sale.customer,
            kind=Followup.Kind.CREDIT_DUE,
            due_date=sale.due_date
            or sale.sale_date + timedelta(days=CREDIT_FOLLOWUP_DAYS),
            amount=sale.amount_due - settled_total(sale),
        )

    if sale.sale_type == Sale.SaleType.MAINTENANCE:
        # Both count from the day of the service, not from today.
        for kind, days in (
            (Followup.Kind.MAINTENANCE_POST, MAINTENANCE_POST_DAYS),
            (Followup.Kind.MAINTENANCE_DUE, MAINTENANCE_FOLLOWUP_DAYS),
        ):
            Followup.objects.create(
                business=sale.business,
                sale=sale,
                customer=sale.customer,
                kind=kind,
                due_date=sale.sale_date + timedelta(days=days),
            )


def installment_rows(sale):
    """
    The EMI schedule with what has been paid on each instalment. Money
    received after the down payment covers the instalments in order.
    """
    pool = max(ZERO, paid_total(sale) - sale.down_payment)
    today = timezone.localdate()
    rows = []

    for installment in sale.installments.all():
        paid = min(pool, installment.amount)
        pool -= paid

        if paid >= installment.amount:
            state = "paid"
        elif installment.due_date < today:
            state = "overdue"
        else:
            state = "partial" if paid > 0 else "upcoming"

        rows.append({
            "id": installment.pk,
            "number": installment.number,
            "due_date": installment.due_date,
            "amount": installment.amount,
            "paid_amount": paid,
            "status": state,
        })

    return rows


def sync_followups(sale):
    """
    Close the follow-ups whose money has now been received, and open again
    the ones closed this way whose payment was voided. Follow-ups that a
    person marked done or skipped are left alone.
    """
    covered = {
        row["id"]: row["status"] == "paid" for row in installment_rows(sale)
    }
    remaining = sale.amount_due - settled_total(sale)
    today = timezone.localdate()

    for followup in sale.followups.exclude(kind__in=MAINTENANCE_KINDS):
        if followup.kind == Followup.Kind.EMI_DUE:
            settled = covered.get(followup.installment_id, False)
        else:
            settled = remaining <= 0

        if settled and followup.status == Followup.Status.PENDING:
            followup.status = Followup.Status.DONE
            followup.auto_closed = True
            followup.completed_on = today
            followup.save()

        elif (
            not settled
            and followup.auto_closed
            and followup.status == Followup.Status.DONE
        ):
            followup.status = Followup.Status.PENDING
            followup.auto_closed = False
            followup.completed_on = None
            followup.save()


def _lock_followup(followup):
    followup = Followup.objects.select_for_update().get(pk=followup.pk)

    if followup.status != Followup.Status.PENDING:
        raise Conflict(
            f"This follow-up is already {followup.status}.",
            code="invalid_status",
        )

    return followup


@transaction.atomic
def complete_followup(*, followup, user, note="", next_due_date=None,
                      has_issue=False):
    """
    Mark a follow-up done. For maintenance, ``next_due_date`` books the next
    follow-up (a new maintenance sale also books one by itself).
    """
    followup = _lock_followup(followup)

    if next_due_date is not None:
        if followup.kind not in MAINTENANCE_KINDS:
            raise serializers.ValidationError({
                "next_due_date": "Only a maintenance follow-up can book a next one."
            })

        if next_due_date <= timezone.localdate():
            raise serializers.ValidationError(
                {"next_due_date": "The next follow-up must be in the future."}
            )

    followup.status = Followup.Status.DONE
    followup.completed_on = timezone.localdate()
    followup.completed_by = user
    followup.note = note
    followup.has_issue = bool(has_issue) and followup.kind in MAINTENANCE_KINDS
    followup.save()

    if next_due_date is not None:
        Followup.objects.create(
            business=followup.business,
            sale=followup.sale,
            customer=followup.customer,
            kind=Followup.Kind.MAINTENANCE_DUE,
            due_date=next_due_date,
        )

    return followup


@transaction.atomic
def skip_followup(*, followup, user, note):
    followup = _lock_followup(followup)

    followup.status = Followup.Status.SKIPPED
    followup.completed_on = timezone.localdate()
    followup.completed_by = user
    followup.note = note
    followup.save()

    return followup


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

@transaction.atomic
def create_payment(*, sale, user, amount, paid_on, method,
                   reference="", notes=""):
    from apps.finance.services import record_sale_income

    sale = Sale.objects.select_for_update().get(pk=sale.pk)

    if sale.status == Sale.Status.CANCELLED:
        raise Conflict("A cancelled sale cannot take payment.", code="invalid_status")

    settled = settled_total(sale)
    amount = money(amount)

    if settled + amount > sale.amount_due:
        raise serializers.ValidationError({
            "amount": (
                f"This is more than is still due ({sale.amount_due - settled})."
            )
        })

    payment = SalePayment.objects.create(
        sale=sale,
        amount=amount,
        paid_on=paid_on,
        method=method,
        reference=reference,
        notes=notes,
        received_by=user,
    )

    record_sale_income(payment)
    sync_followups(sale)

    return payment


@transaction.atomic
def void_payment(*, payment, user, reason):
    from apps.finance.services import void_sale_income

    payment = SalePayment.objects.select_for_update().get(pk=payment.pk)
    sale = Sale.objects.select_for_update().get(pk=payment.sale_id)

    if payment.status != SalePayment.Status.ACTIVE:
        raise Conflict("This payment is already void.", code="invalid_status")

    if sale.returns.filter(status="active").exists():
        raise Conflict(
            "This sale has returned goods. Void the return first.",
            code="returns_exist",
        )

    left = paid_total(sale) - payment.amount

    if (
        sale.status == Sale.Status.CONFIRMED
        and sale.customer_id is None
        and left < sale.amount_due
    ):
        raise Conflict(
            "This walk-in sale would be left unpaid, with no customer to "
            "collect from.",
            code="customer_required",
        )

    if sale.status == Sale.Status.CONFIRMED and left < sale.down_payment:
        raise Conflict(
            "This payment is part of the down payment of a confirmed EMI "
            "sale.",
            code="down_payment_due",
        )

    payment.status = SalePayment.Status.VOID
    payment.voided_by = user
    payment.voided_at = timezone.now()
    payment.void_reason = reason
    payment.save(update_fields=["status", "voided_by", "voided_at", "void_reason"])

    void_sale_income(payment, user, reason)
    sync_followups(sale)

    return payment


# ---------------------------------------------------------------------------
# Bike units
# ---------------------------------------------------------------------------

@transaction.atomic
def register_bike_unit(*, business, product, **fields):
    """
    Add a bike to the business's units. There can never be more units in
    stock than the stock balance of the model, so register units after the
    bikes have been received into stock.
    """
    Business.objects.select_for_update().get(pk=business.pk)

    balance = (
        StockBalance.objects.filter(business=business, product=product)
        .values_list("quantity", flat=True)
        .first()
    ) or Decimal("0")

    in_stock = BikeUnit.objects.filter(
        business=business, product=product, status=BikeUnit.Status.IN_STOCK
    ).count()

    if in_stock + 1 > balance:
        raise Conflict(
            f"{product.name}: {balance} in stock and {in_stock} already "
            "registered. Receive the bike into stock first.",
            code="not_in_stock",
        )

    return BikeUnit.objects.create(business=business, product=product, **fields)
