from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum

from apps.businesses.models import Business
from apps.inventory.models import (
    StockBalance,
    StockMovement,
    StockTransfer,
    StockTransferReceiptItem,
    StockTransferReturn,
    StockTransferReturnItem,
)
from apps.inventory.services.stock import validate_quantity
from apps.inventory.services.validators import validate_inventory_access


ZERO = Decimal("0.000")


def _get_damaged_received_quantity(transfer_item):
    return (
        StockTransferReceiptItem.objects
        .filter(transfer_item=transfer_item)
        .aggregate(total=Sum("damaged_quantity"))["total"]
        or ZERO
    )


def _get_returned_quantity(transfer_item):
    return (
        StockTransferReturnItem.objects
        .filter(transfer_item=transfer_item)
        .aggregate(total=Sum("quantity"))["total"]
        or ZERO
    )


@transaction.atomic
def return_damaged_stock_transfer(
    *,
    user,
    transfer_id,
    items,
    reference,
    notes="",
):
    """
    Dispatch damaged stock from a Branch back to its Head Office.

    Business rules:
    - Only Branch users with full write access can return stock.
    - Only damaged quantities previously received can be returned.
    - Partial returns and multiple return batches are supported.
    - Branch damaged stock decreases immediately.
    - Head Office stock does not change at this stage.
    - Every returned item creates an immutable stock movement.
    - All changes roll back if any operation fails.
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
            "At least one return item is required."
        )

    if not isinstance(reference, str) or not reference.strip():
        raise ValidationError("Return reference is required.")

    reference = reference.strip()

    if len(reference) > 100:
        raise ValidationError(
            "Return reference cannot exceed 100 characters."
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
        StockTransfer.Status.PARTIALLY_RECEIVED,
        StockTransfer.Status.RECEIVED,
    )

    if transfer.status == StockTransfer.Status.CANCELLED:
        raise ValidationError(
            "Cancelled transfers cannot be returned."
        )

    if transfer.status not in allowed_statuses:
        raise ValidationError(
            "Damaged stock can only be returned after Branch receipt."
        )

    # --------------------------------------------------
    # LOCK BUSINESSES IN CONSISTENT ORDER
    # --------------------------------------------------

    business_ids = sorted({
        transfer.source_business_id,
        transfer.destination_business_id,
    })

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
        or branch.business_type
        != Business.BusinessType.BRANCH
        or branch.parent_business_id != head_office.pk
        or branch.company_id != head_office.company_id
    ):
        raise ValidationError(
            "Invalid Head Office to Branch transfer."
        )

    if not head_office.is_active or not branch.is_active:
        raise ValidationError(
            "Head Office or Branch is inactive."
        )

    # --------------------------------------------------
    # ACCESS VALIDATION
    # --------------------------------------------------

    validate_inventory_access(
        user,
        branch,
        write=True,
    )

    # --------------------------------------------------
    # VALIDATE RETURN ITEMS
    # --------------------------------------------------

    prepared_items = []
    seen_item_ids = set()

    for item in items:

        if not isinstance(item, dict):
            raise ValidationError(
                "Each return item must be an object."
            )

        transfer_item_id = item.get("transfer_item_id")

        if (
            not isinstance(transfer_item_id, int)
            or isinstance(transfer_item_id, bool)
            or transfer_item_id <= 0
        ):
            raise ValidationError(
                "Invalid transfer item ID."
            )

        if transfer_item_id in seen_item_ids:
            raise ValidationError(
                f"Duplicate transfer item ID: {transfer_item_id}."
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

        damaged_received = _get_damaged_received_quantity(
            transfer_item
        )

        already_returned = _get_returned_quantity(
            transfer_item
        )

        remaining_returnable = (
            damaged_received - already_returned
        )

        if remaining_returnable <= ZERO:
            raise ValidationError(
                f"No damaged quantity remains returnable for "
                f"{transfer_item.product.name}."
            )

        if quantity > remaining_returnable:
            raise ValidationError(
                f"Return quantity exceeds remaining damaged "
                f"quantity for {transfer_item.product.name}. "
                f"Remaining: {remaining_returnable}."
            )

        prepared_items.append(
            (transfer_item, quantity)
        )

    # --------------------------------------------------
    # LOCK BRANCH STOCK BALANCES
    # --------------------------------------------------

    product_ids = sorted({
        transfer_item.product_id
        for transfer_item, _ in prepared_items
    })

    balances = {}

    for product_id in product_ids:

        balance = (
            StockBalance.objects
            .select_for_update()
            .filter(
                product_id=product_id,
                business=branch,
            )
            .first()
        )

        if balance is None:
            raise ValidationError(
                "Branch stock balance was not found for a returned product."
            )

        balances[product_id] = balance

    # --------------------------------------------------
    # CHECK ALL DAMAGED STOCK BEFORE WRITING
    # --------------------------------------------------

    for transfer_item, quantity in prepared_items:

        balance = balances[transfer_item.product_id]

        if balance.damaged_quantity < quantity:
            raise ValidationError(
                f"Insufficient DAMAGED stock for "
                f"{transfer_item.product.name}. "
                f"Available: {balance.damaged_quantity}, "
                f"requested: {quantity}."
            )

    # --------------------------------------------------
    # CREATE RETURN EVENT
    # --------------------------------------------------

    return_batch = StockTransferReturn.objects.create(
        transfer=transfer,
        reference=reference,
        notes=notes.strip(),
        dispatched_by=user,
    )

    # --------------------------------------------------
    # REDUCE BRANCH DAMAGED STOCK AND CREATE HISTORY
    # --------------------------------------------------

    for transfer_item, quantity in prepared_items:

        balance = balances[transfer_item.product_id]

        balance_before = balance.damaged_quantity
        balance_after = balance_before - quantity

        return_item = StockTransferReturnItem.objects.create(
            return_batch=return_batch,
            transfer_item=transfer_item,
            quantity=quantity,
        )

        balance.damaged_quantity = balance_after

        balance.save(
            update_fields=[
                "damaged_quantity",
                "updated_at",
            ]
        )

        StockMovement.objects.create(
            stock_balance=balance,
            stock_condition=StockMovement.StockCondition.DAMAGED,
            movement_type=StockMovement.MovementType.TRANSFER_OUT,
            quantity=quantity,
            balance_before=balance_before,
            balance_after=balance_after,
            transfer_return_item=return_item,
            reference=reference,
            notes=f"Damaged stock return: {transfer.reference}",
            created_by=user,
        )

    return return_batch