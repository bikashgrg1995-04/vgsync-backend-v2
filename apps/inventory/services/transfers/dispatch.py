
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.businesses.models import Business
from apps.inventory.models import (
    StockBalance,
    StockMovement,
    StockTransfer,
    StockTransferDispatch,
    StockTransferDispatchItem,
    StockTransferReceiptItem,
)
from apps.inventory.services.stock import validate_quantity
from apps.inventory.services.validators import (
    validate_inventory_access,
)


ZERO = Decimal("0.000")


def _get_dispatched_quantity(transfer_item):
    return (
        StockTransferDispatchItem.objects
        .filter(transfer_item=transfer_item)
        .aggregate(total=Sum("quantity"))["total"]
        or ZERO
    )


def _get_received_quantity(transfer_item):
    totals = (
        StockTransferReceiptItem.objects
        .filter(transfer_item=transfer_item)
        .aggregate(
            good=Sum("good_quantity"),
            damaged=Sum("damaged_quantity"),
        )
    )

    return (
        (totals["good"] or ZERO)
        + (totals["damaged"] or ZERO)
    )


def _update_transfer_status(transfer):
    """
    Recalculate transfer status from all ordered, dispatched
    and received quantities.
    """

    transfer_items = list(
        transfer.items.select_for_update().all()
    )

    total_ordered = ZERO
    total_dispatched = ZERO
    total_received = ZERO

    for transfer_item in transfer_items:
        total_ordered += transfer_item.ordered_quantity
        total_dispatched += _get_dispatched_quantity(transfer_item)
        total_received += _get_received_quantity(transfer_item)

    if total_ordered <= ZERO:
        raise ValidationError(
            "Transfer must contain at least one order item."
        )

    if total_received > total_dispatched:
        raise ValidationError(
            "Received quantity cannot exceed dispatched quantity."
        )

    if total_dispatched > total_ordered:
        raise ValidationError(
            "Dispatched quantity cannot exceed ordered quantity."
        )

    if total_received == total_ordered:
        transfer.status = StockTransfer.Status.RECEIVED

    elif total_received > ZERO:
        transfer.status = StockTransfer.Status.PARTIALLY_RECEIVED

    elif total_dispatched == total_ordered:
        transfer.status = StockTransfer.Status.DISPATCHED

    elif total_dispatched > ZERO:
        transfer.status = StockTransfer.Status.PARTIALLY_DISPATCHED

    else:
        transfer.status = StockTransfer.Status.ORDERED

    transfer.updated_at = timezone.now()

    transfer.save(
        update_fields=["status", "updated_at"]
    )


@transaction.atomic
def dispatch_stock_transfer(
    *,
    user,
    transfer_id,
    items,
    reference,
    notes="",
):
    """
    Dispatch ordered stock from Head Office to Branch.

    Business rules:
    - Only Head Office full-access users can dispatch.
    - Only GOOD stock is dispatched.
    - Partial dispatch is supported.
    - Multiple dispatch batches are supported.
    - Dispatch after partial receipt is supported.
    - Total dispatched quantity cannot exceed ordered quantity.
    - Every dispatch creates immutable event and movement history.
    - All stock changes roll back if any operation fails.
    """

    # --------------------------------------------------
    # BASIC INPUT VALIDATION
    # --------------------------------------------------

    if (
        not isinstance(transfer_id, int)
        or isinstance(transfer_id, bool)
        or transfer_id <= 0
    ):
        raise ValidationError("Invalid transfer ID.")

    if not isinstance(items, (list, tuple)) or not items:
        raise ValidationError(
            "At least one dispatch item is required."
        )

    if not isinstance(reference, str) or not reference.strip():
        raise ValidationError("Dispatch reference is required.")

    reference = reference.strip()

    if len(reference) > 100:
        raise ValidationError(
            "Dispatch reference cannot exceed 100 characters."
        )

    if not isinstance(notes, str):
        raise ValidationError("Notes must be a string.")

    # --------------------------------------------------
    # LOCK TRANSFER
    # --------------------------------------------------

    transfer = (
        StockTransfer.objects
        .select_for_update()
        .get(pk=transfer_id)
    )

    allowed_statuses = (
        StockTransfer.Status.ORDERED,
        StockTransfer.Status.PARTIALLY_DISPATCHED,
        StockTransfer.Status.PARTIALLY_RECEIVED,
    )

    if transfer.status == StockTransfer.Status.CANCELLED:
        raise ValidationError(
            "Cancelled transfers cannot be dispatched."
        )

    if transfer.status not in allowed_statuses:
        raise ValidationError(
            "This transfer cannot be dispatched in its current status."
        )

    # --------------------------------------------------
    # LOCK BUSINESSES IN CONSISTENT ORDER
    # --------------------------------------------------

    business_ids = sorted([
        transfer.source_business_id,
        transfer.destination_business_id,
    ])

    locked_businesses = {
        business.pk: business
        for business in (
            Business.objects
            .select_for_update()
            .filter(pk__in=business_ids)
            .order_by("pk")
        )
    }

    head_office = locked_businesses.get(
        transfer.source_business_id
    )

    branch = locked_businesses.get(
        transfer.destination_business_id
    )

    if head_office is None or branch is None:
        raise ValidationError(
            "Transfer source or destination business does not exist."
        )

    # --------------------------------------------------
    # VALIDATE TRANSFER RELATIONSHIP
    # --------------------------------------------------

    if (
        head_office.business_type
        != Business.BusinessType.HEAD_OFFICE
    ):
        raise ValidationError(
            "Transfer source must be a Head Office."
        )

    if (
        branch.business_type
        != Business.BusinessType.BRANCH
    ):
        raise ValidationError(
            "Transfer destination must be a Branch."
        )

    if (
        branch.parent_business_id != head_office.pk
        or branch.company_id != head_office.company_id
    ):
        raise ValidationError(
            "Invalid Head Office and Branch relationship."
        )

    if not head_office.is_active or not branch.is_active:
        raise ValidationError(
            "Source or destination business is inactive."
        )

    # --------------------------------------------------
    # ACCESS VALIDATION
    # --------------------------------------------------

    validate_inventory_access(
        user,
        head_office,
        write=True,
    )

    # --------------------------------------------------
    # VALIDATE DISPATCH ITEMS
    # --------------------------------------------------

    prepared_items = []
    seen_item_ids = set()

    for item in items:

        if not isinstance(item, dict):
            raise ValidationError(
                "Each dispatch item must be an object."
            )

        transfer_item_id = item.get("transfer_item_id")

        if (
            isinstance(transfer_item_id, bool)
            or not isinstance(transfer_item_id, int)
            or transfer_item_id <= 0
        ):
            raise ValidationError(
                "Invalid transfer item ID."
            )

        if transfer_item_id in seen_item_ids:
            raise ValidationError(
                "A transfer item cannot appear more than once."
            )

        seen_item_ids.add(transfer_item_id)

        quantity = validate_quantity(
            item.get("quantity")
        )

        transfer_item = (
            transfer.items
            .select_for_update()
            .select_related("product")
            .filter(pk=transfer_item_id)
            .first()
        )

        if transfer_item is None:
            raise ValidationError(
                f"Transfer item {transfer_item_id} "
                "does not belong to this order."
            )

        if (
            transfer_item.product.owner_business_id
            != head_office.pk
        ):
            raise ValidationError(
                "Transfer product does not belong to this Head Office."
            )

        if not transfer_item.product.is_active:
            raise ValidationError(
                f"Product {transfer_item.product.name} is inactive."
            )

        dispatched_before = _get_dispatched_quantity(
            transfer_item
        )

        remaining_quantity = (
            transfer_item.ordered_quantity
            - dispatched_before
        )

        if remaining_quantity <= ZERO:
            raise ValidationError(
                f"Product {transfer_item.product.name} "
                "has already been fully dispatched."
            )

        if quantity > remaining_quantity:
            raise ValidationError(
                f"Dispatch quantity exceeds remaining ordered "
                f"quantity for {transfer_item.product.name}. "
                f"Remaining: {remaining_quantity}."
            )

        prepared_items.append(
            (transfer_item, quantity)
        )

    # --------------------------------------------------
    # LOCK STOCK BALANCES
    # --------------------------------------------------

    product_ids = sorted({
        transfer_item.product_id
        for transfer_item, _ in prepared_items
    })

    balances = {}

    for product_id in product_ids:

        balance, _ = (
            StockBalance.objects
            .select_for_update()
            .get_or_create(
                product_id=product_id,
                business=head_office,
                defaults={
                    "quantity": ZERO,
                    "damaged_quantity": ZERO,
                },
            )
        )

        balances[product_id] = balance

    # --------------------------------------------------
    # CHECK ALL STOCK BEFORE WRITING
    # --------------------------------------------------

    for transfer_item, quantity in prepared_items:

        balance = balances[transfer_item.product_id]

        if balance.quantity < quantity:
            raise ValidationError(
                f"Insufficient GOOD stock for "
                f"{transfer_item.product.name}. "
                f"Available: {balance.quantity}, "
                f"requested: {quantity}."
            )

    # --------------------------------------------------
    # CREATE DISPATCH EVENT
    # --------------------------------------------------

    dispatch = StockTransferDispatch.objects.create(
        transfer=transfer,
        reference=reference,
        notes=notes.strip(),
        dispatched_by=user,
    )

    # --------------------------------------------------
    # UPDATE STOCK AND CREATE MOVEMENT HISTORY
    # --------------------------------------------------

    for transfer_item, quantity in prepared_items:

        balance = balances[transfer_item.product_id]

        balance_before = balance.quantity
        balance_after = balance_before - quantity

        dispatch_item = StockTransferDispatchItem.objects.create(
            dispatch=dispatch,
            transfer_item=transfer_item,
            quantity=quantity,
        )

        balance.quantity = balance_after

        balance.save(
            update_fields=[
                "quantity",
                "updated_at",
            ]
        )

        StockMovement.objects.create(
            stock_balance=balance,
            stock_condition=StockMovement.StockCondition.GOOD,
            movement_type=StockMovement.MovementType.TRANSFER_OUT,
            quantity=quantity,
            balance_before=balance_before,
            balance_after=balance_after,
            transfer_dispatch_item=dispatch_item,
            reference=reference,
            notes=f"Transfer dispatch: {transfer.reference}",
            created_by=user,
        )

    # --------------------------------------------------
    # UPDATE TRANSFER STATUS
    # --------------------------------------------------

    _update_transfer_status(transfer)

    return dispatch
