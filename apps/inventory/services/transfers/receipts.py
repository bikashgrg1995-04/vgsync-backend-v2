
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.businesses.models import Business
from apps.inventory.models import (
    StockBalance,
    StockMovement,
    StockTransfer,
    StockTransferReceipt,
    StockTransferReceiptItem,
)
from apps.inventory.services.stock import validate_quantity
from apps.inventory.services.transfers.dispatch import (
    _get_dispatched_quantity,
    _get_received_quantity,
    _update_transfer_status,
)
from apps.inventory.services.validators import (
    validate_inventory_access,
)


ZERO = Decimal("0.000")


def _validate_receipt_quantity(value, field_name):
    """
    Validate a non-negative receipt quantity.

    Unlike dispatch quantities, receipt quantities may be zero
    because a received item can be entirely good or entirely damaged.
    """

    if isinstance(value, bool) or not isinstance(value, (str, Decimal)):
        raise ValidationError(
            f"{field_name} must be a decimal string or Decimal."
        )

    try:
        parsed = Decimal(value)
    except Exception as exc:
        raise ValidationError(
            f"{field_name} must be a valid decimal quantity."
        ) from exc

    if not parsed.is_finite():
        raise ValidationError(
            f"{field_name} must be a finite quantity."
        )

    if parsed < ZERO:
        raise ValidationError(
            f"{field_name} cannot be negative."
        )

    if parsed == ZERO:
        decimal_places = max(-parsed.as_tuple().exponent, 0)
        digits = len(parsed.as_tuple().digits)
        whole_digits = max(
            digits + parsed.as_tuple().exponent,
            0,
        )

        if decimal_places > 3 or whole_digits > 11:
            raise ValidationError(
                f"{field_name} exceeds the allowed precision."
            )

        return ZERO

    return validate_quantity(value)


@transaction.atomic
def receive_stock_transfer(
    *,
    user,
    transfer_id,
    items,
    reference,
    notes="",
):
    """
    Record stock physically received by a Branch from its Head Office.

    Business rules:
    - Only Branch users with full write access can receive.
    - Only previously dispatched quantities can be received.
    - Partial receipt and multiple receipt batches are supported.
    - Good and damaged quantities are recorded separately.
    - Good stock increases Branch good stock.
    - Damaged stock increases Branch damaged stock.
    - Every positive condition quantity creates an immutable movement.
    - Receiving does not change Head Office stock.
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
            "At least one receipt item is required."
        )

    if not isinstance(reference, str) or not reference.strip():
        raise ValidationError("Receipt reference is required.")

    reference = reference.strip()

    if len(reference) > 100:
        raise ValidationError(
            "Receipt reference cannot exceed 100 characters."
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
        StockTransfer.Status.PARTIALLY_DISPATCHED,
        StockTransfer.Status.DISPATCHED,
        StockTransfer.Status.PARTIALLY_RECEIVED,
    )

    if transfer.status == StockTransfer.Status.CANCELLED:
        raise ValidationError(
            "Cancelled transfers cannot be received."
        )

    if transfer.status not in allowed_statuses:
        raise ValidationError(
            "This transfer does not have any receivable stock."
        )

    # --------------------------------------------------
    # LOCK BUSINESSES
    # --------------------------------------------------

    business_ids = sorted({
        transfer.source_business_id,
        transfer.destination_business_id,
    })

    locked_businesses = list(
        Business.objects
        .select_for_update()
        .filter(pk__in=business_ids)
        .order_by("pk")
    )

    businesses = {
        business.pk: business
        for business in locked_businesses
    }

    head_office = businesses.get(
        transfer.source_business_id
    )
    branch = businesses.get(
        transfer.destination_business_id
    )

    if head_office is None or branch is None:
        raise ValidationError(
            "Transfer source or destination business was not found."
        )

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
    # VALIDATE RECEIPT ITEMS
    # --------------------------------------------------

    prepared_items = []
    seen_item_ids = set()

    for item in items:

        if not isinstance(item, dict):
            raise ValidationError(
                "Each receipt item must be an object."
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

        good_quantity = _validate_receipt_quantity(
            item.get("good_quantity", "0"),
            "Good quantity",
        )

        damaged_quantity = _validate_receipt_quantity(
            item.get("damaged_quantity", "0"),
            "Damaged quantity",
        )

        total_quantity = good_quantity + damaged_quantity

        if total_quantity <= ZERO:
            raise ValidationError(
                "At least one of good or damaged quantity "
                "must be greater than zero."
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

        dispatched_quantity = _get_dispatched_quantity(
            transfer_item
        )

        received_quantity = _get_received_quantity(
            transfer_item
        )

        remaining_quantity = (
            dispatched_quantity - received_quantity
        )

        if remaining_quantity <= ZERO:
            raise ValidationError(
                f"No dispatched quantity remains to receive for "
                f"{transfer_item.product.name}."
            )

        if total_quantity > remaining_quantity:
            raise ValidationError(
                f"Receipt quantity exceeds remaining dispatched "
                f"quantity for {transfer_item.product.name}. "
                f"Remaining: {remaining_quantity}."
            )

        prepared_items.append(
            (
                transfer_item,
                good_quantity,
                damaged_quantity,
            )
        )

    # --------------------------------------------------
    # LOCK BRANCH STOCK BALANCES
    # --------------------------------------------------

    product_ids = sorted({
        transfer_item.product_id
        for transfer_item, _, _ in prepared_items
    })

    balances = {}

    for product_id in product_ids:

        balance, _ = (
            StockBalance.objects
            .select_for_update()
            .get_or_create(
                product_id=product_id,
                business=branch,
                defaults={
                    "quantity": ZERO,
                    "damaged_quantity": ZERO,
                },
            )
        )

        balances[product_id] = balance

    # --------------------------------------------------
    # CREATE RECEIPT EVENT
    # --------------------------------------------------

    receipt = StockTransferReceipt.objects.create(
        transfer=transfer,
        reference=reference,
        notes=notes.strip(),
        received_by=user,
    )

    # --------------------------------------------------
    # UPDATE STOCK AND CREATE MOVEMENT HISTORY
    # --------------------------------------------------

    for (
        transfer_item,
        good_quantity,
        damaged_quantity,
    ) in prepared_items:

        balance = balances[transfer_item.product_id]

        receipt_item = StockTransferReceiptItem.objects.create(
            receipt=receipt,
            transfer_item=transfer_item,
            good_quantity=good_quantity,
            damaged_quantity=damaged_quantity,
        )

        # ----------------------------------------------
        # GOOD STOCK
        # ----------------------------------------------

        if good_quantity > ZERO:

            balance_before = balance.quantity
            balance_after = balance_before + good_quantity

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
                movement_type=StockMovement.MovementType.TRANSFER_IN,
                quantity=good_quantity,
                balance_before=balance_before,
                balance_after=balance_after,
                transfer_receipt_item=receipt_item,
                reference=reference,
                notes=f"Transfer receipt: {transfer.reference}",
                created_by=user,
            )

        # ----------------------------------------------
        # DAMAGED STOCK
        # ----------------------------------------------

        if damaged_quantity > ZERO:

            balance_before = balance.damaged_quantity
            balance_after = balance_before + damaged_quantity

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
                movement_type=StockMovement.MovementType.TRANSFER_IN,
                quantity=damaged_quantity,
                balance_before=balance_before,
                balance_after=balance_after,
                transfer_receipt_item=receipt_item,
                reference=reference,
                notes=f"Damaged transfer receipt: {transfer.reference}",
                created_by=user,
            )

    # --------------------------------------------------
    # UPDATE TRANSFER STATUS
    # --------------------------------------------------

    _update_transfer_status(transfer)

    return receipt
