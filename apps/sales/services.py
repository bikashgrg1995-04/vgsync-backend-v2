"""
Sales rules in one place: totals, numbering, issuing stock, payments.

Every total on a sale comes from ``calculate_totals`` and is recalculated
whenever the lines change; the client never sends a total.
"""

import calendar
from datetime import timedelta
from decimal import ROUND_UP, Decimal

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
    FreeService,
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
    # ``product`` may be a catalog Product or a LocalProduct: both have a
    # default sale price.
    if price is None and product is not None:
        price = product.sale_price

    return money(price) if price is not None else None


def _resolve_items(business, sale_type, items, kind="paid"):
    """
    Turn the lines sent by the client into lines ready to store, checking
    them against the kind of sale:

    * stock: products, and local products (sales-only, no stock).
    * bike: bike units only, one each, in stock in this business.
    * maintenance: stock parts, local products, and service charges (a
      description, no product, nothing taken from stock).
    """
    resolved = []
    seen_units = set()
    # A free service is mostly no charge; an accident repair is priced later.
    zero_ok = sale_type == Sale.SaleType.MAINTENANCE and kind in (
        Sale.ServiceKind.FREE,
        Sale.ServiceKind.ACCIDENT,
    )

    if not items and kind != Sale.ServiceKind.ACCIDENT:
        raise serializers.ValidationError({"items": "Add at least one item."})

    for entry in items:
        product = entry.get("product")
        local = entry.get("local_product")
        unit = entry.get("bike_unit")
        description = (entry.get("description") or "").strip()
        quantity = entry.get("quantity")

        if sale_type == Sale.SaleType.BIKE:
            if unit is None or product is not None or local is not None or description:
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

            if local is not None:
                if product is not None or description:
                    raise serializers.ValidationError({
                        "items": (
                            "A line is a product, a local product or a "
                            "service charge, not a mix."
                        )
                    })

                # The line is named after the local product.
                description = local.name

            elif product is None and sale_type != Sale.SaleType.MAINTENANCE:
                raise serializers.ValidationError(
                    {"items": "Choose a product for every line."}
                )

            elif product is None and not description:
                raise serializers.ValidationError({
                    "items": "A service charge needs a description."
                })

            elif product is not None and description:
                raise serializers.ValidationError({
                    "items": "A line is a product or a service charge, not both."
                })

        price = _price(entry.get("unit_price"), product or local)

        if price is None or price < 0 or (price == 0 and not zero_ok):
            what = (
                product.name if product else (description or "This line")
            )
            raise serializers.ValidationError({
                "items": f"{what} has no price. Enter a price for this line."
            })

        resolved.append({
            "product": product,
            "local_product": local,
            "bike_unit": unit,
            "description": description,
            "quantity": quantity,
            "unit_price": price,
        })

    return resolved


def monthly_instalment(principal, yearly_rate, months):
    """
    The EMI on a reducing balance: interest is charged each month on what is
    still owed, at yearly_rate / 12. With r the monthly rate and n the
    months, EMI = P x r x (1+r)^n / ((1+r)^n - 1). No interest just divides
    the amount. Rounded up to the paisa so the instalments never fall short.
    """
    principal = Decimal(principal)
    rate = Decimal(yearly_rate) / 12 / 100

    if rate == 0:
        each = principal / months
    else:
        growth = (1 + rate) ** months
        each = principal * rate * growth / (growth - 1)

    return each.quantize(Decimal("0.01"), rounding=ROUND_UP)


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
            or sale.interest_rate is not None
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

    if not sale.emi_months:
        raise serializers.ValidationError({
            "emi_months": "Give the number of months."
        })

    sale.down_payment = money(sale.down_payment or 0)

    if sale.down_payment > net_total:
        raise serializers.ValidationError(
            {"down_payment": "The down payment cannot be more than the total."}
        )

    if sale.interest_rate is not None or not sale.emi_amount:
        # Worked out here, so it always matches the total: reducing-
        # balance EMI on what is left after the down payment.
        sale.interest_rate = Decimal(sale.interest_rate or 0)
        sale.emi_amount = monthly_instalment(
            net_total - sale.down_payment, sale.interest_rate, sale.emi_months
        )

    sale.emi_amount = money(sale.emi_amount)

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
            local_product=item["local_product"],
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


def _check_service_kind(sale):
    """Rules of the maintenance sub-types (normal, free, accident)."""
    kind = sale.service_kind
    field = {"service_kind": "This is for maintenance sales."}

    if sale.sale_type != Sale.SaleType.MAINTENANCE:
        if kind != Sale.ServiceKind.PAID:
            raise serializers.ValidationError(field)

        if sale.free_service_for_id or sale.odometer_km is not None:
            raise serializers.ValidationError(
                {"odometer_km": "The bike's km and free service are for maintenance."}
            )

        return

    if kind != Sale.ServiceKind.FREE:
        if sale.free_service_for_id:
            raise serializers.ValidationError({
                "free_service_for": "Only a free service is linked to a bike sale."
            })

        return

    original = sale.free_service_for

    if original is None:
        raise serializers.ValidationError({
            "free_service_for": "Choose the bike sale this free service belongs to."
        })

    if (
        original.business_id != sale.business_id
        or original.sale_type != Sale.SaleType.BIKE
        or original.status != Sale.Status.CONFIRMED
    ):
        raise serializers.ValidationError({
            "free_service_for": "Choose a confirmed bike sale of this business."
        })

    if sale.odometer_km is None:
        raise serializers.ValidationError(
            {"odometer_km": "Give the km the bike has run."}
        )

    done = original.free_service_records.count()

    if done >= original.free_services:
        raise serializers.ValidationError({
            "free_service_for": (
                "All free services of that sale are already done."
                if original.free_services
                else "That sale has no free services included."
            )
        })

    # The job is for the same customer and bike as the sale.
    if sale.customer_id is None and not sale.walk_in_name:
        sale.customer_id = original.customer_id
        sale.walk_in_name = original.walk_in_name

    if not (sale.vehicle_chassis_no or sale.vehicle_reg_no):
        unit_item = original.items.select_related("bike_unit").first()

        if unit_item and unit_item.bike_unit_id:
            sale.vehicle_chassis_no = unit_item.bike_unit.chassis_no


def _check_free_services(sale):
    if (
        sale.free_services or sale.free_service_covers
    ) and sale.sale_type != Sale.SaleType.BIKE:
        raise serializers.ValidationError(
            {"free_services": "Free services are only for bike sales."}
        )


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

    _check_free_services(sale)
    _check_service_kind(sale)
    items = _resolve_items(business, sale.sale_type, items, sale.service_kind)

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

    _check_free_services(sale)
    _check_service_kind(sale)

    if items is None:
        lines = [(i.quantity, i.unit_price) for i in sale.items.all()]
    else:
        items = _resolve_items(
            sale.business, sale.sale_type, items, sale.service_kind
        )
        lines = [(i["quantity"], i["unit_price"]) for i in items]

    totals = calculate_totals(lines, sale.discount_amount, sale.vat_rate)

    for field, value in _header_fields(totals).items():
        setattr(sale, field, value)

    sale.finance_charge = _check_terms(sale, totals["net_total"])
    paid = settled_total(sale)

    provisional = sale.service_kind == Sale.ServiceKind.ACCIDENT and sale.amount_due == 0

    if sale.amount_due < paid and not provisional:
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
def confirm_sale(*, sale, user, ignore_documents=False):
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

    if sale.service_kind == Sale.ServiceKind.ACCIDENT:
        if sale.amount_due <= 0:
            raise serializers.ValidationError({
                "items": "Enter the final price of the repair before billing."
            })

        if paid > sale.amount_due:
            raise Conflict(
                f"The advance ({paid}) is more than the final total "
                f"({sale.amount_due}). Raise the total or void a payment.",
                code="advance_exceeds_total",
            )

        documents = list(sale.documents.all())

        if not documents or not all(d.verified for d in documents):
            if not ignore_documents:
                raise Conflict(
                    "The accident papers are not all uploaded and verified."
                    if not documents
                    else "Some accident papers are not verified yet.",
                    code="documents_unverified",
                )

            sale.documents_ignored = True
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

    if sale.service_kind == Sale.ServiceKind.FREE:
        _log_free_job(sale, user)

    _create_followups(sale)
    sync_followups(sale)

    return sale


def _log_free_job(sale, user):
    """Write the job into the free-service record of the bike sale."""
    work = "; ".join(
        i.description or (i.product.name if i.product_id else "")
        for i in sale.items.select_related("product")
    )
    record = add_free_service(
        sale=sale.free_service_for,
        user=user,
        odometer_km=sale.odometer_km,
        service_date=sale.sale_date,
        performance="",
        work_done=work[:2000],
    )
    record.service_sale = sale
    record.save(update_fields=["service_sale"])


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

    # An accident repair has no final total yet, so its advance is not held
    # to the provisional amount; the final bill is checked at confirm.
    advance_first = (
        sale.service_kind == Sale.ServiceKind.ACCIDENT
        and sale.status == Sale.Status.DRAFT
    )

    if not advance_first and settled + amount > sale.amount_due:
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


# ---------------------------------------------------------------------------
# Free services on a bike sale
# ---------------------------------------------------------------------------

def _free_service_sale(sale):
    sale = Sale.objects.select_for_update().get(pk=sale.pk)

    if sale.sale_type != Sale.SaleType.BIKE:
        raise serializers.ValidationError("Free services are only for bike sales.")

    if sale.status != Sale.Status.CONFIRMED:
        raise serializers.ValidationError(
            "Free services can be recorded once the bike sale is confirmed."
        )

    return sale


@transaction.atomic
def add_free_service(
    *, sale, user, odometer_km, service_date=None, performance="", work_done="",
    technician="",
):
    """Record the next free service. Checks the count, the date and the km."""
    from django.utils import timezone

    sale = _free_service_sale(sale)
    records = list(sale.free_service_records.order_by("number"))

    if len(records) >= sale.free_services:
        raise serializers.ValidationError(
            f"All {sale.free_services} free services of this sale are already done."
            if sale.free_services
            else "This sale has no free services included."
        )

    today = timezone.localdate()
    service_date = service_date or today

    if service_date > today:
        raise serializers.ValidationError(
            {"service_date": "The date cannot be in the future."}
        )

    if service_date < sale.sale_date:
        raise serializers.ValidationError(
            {"service_date": "The service cannot be before the sale date."}
        )

    if records:
        last = records[-1]

        if service_date < last.service_date:
            raise serializers.ValidationError(
                {"service_date": "This cannot be before the previous free service."}
            )

        if odometer_km < last.odometer_km:
            raise serializers.ValidationError({
                "odometer_km": (
                    f"The km cannot be less than the previous service "
                    f"({last.odometer_km} km)."
                )
            })

    return FreeService.objects.create(
        business=sale.business,
        sale=sale,
        number=(records[-1].number + 1) if records else 1,
        service_date=service_date,
        odometer_km=odometer_km,
        performance=performance,
        work_done=work_done,
        technician=technician,
        recorded_by=user,
    )


@transaction.atomic
def remove_free_service(*, sale, record_id):
    """Only the latest record can be removed, so the numbering stays in order."""
    sale = _free_service_sale(sale)
    last = sale.free_service_records.order_by("-number").first()

    if last is None or last.pk != record_id:
        raise serializers.ValidationError(
            "Only the latest free service can be removed."
        )

    last.delete()


@transaction.atomic
def set_free_service_count(*, sale, count, covers=None):
    sale = _free_service_sale(sale)
    used = sale.free_service_records.count()

    if count < used:
        raise serializers.ValidationError({
            "free_services": f"{used} free services are already done; it cannot be less."
        })

    sale.free_services = count
    fields = ["free_services", "updated_at"]

    if covers is not None:
        sale.free_service_covers = covers
        fields.append("free_service_covers")

    sale.save(update_fields=fields)

    return sale
