
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.businesses.models import Business
from apps.inventory.models import (
    StockTransfer,
    StockTransferReturnReceipt,
    StockTransferReturnReceiptItem,
    StockTransferReturnReview,
    StockTransferReturnReviewItem,
)
from apps.inventory.services.validators import (
    validate_business_available,
    validate_inventory_access,
)


ZERO = Decimal("0.000")


def _validate_review_quantity(value, field_name):
    """
    Validate a non-negative decimal quantity.

    Accepts Decimal or string only.
    Rejects int, float, bool, non-finite values and
    quantities with more than 3 decimal places.
    """
    if isinstance(value, bool) or not isinstance(value, (str, Decimal)):
        raise ValidationError(
            {field_name: "Quantity must be a decimal string or Decimal."}
        )

    try:
        quantity = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError(
            {field_name: "Enter a valid decimal quantity."}
        )

    if not quantity.is_finite():
        raise ValidationError(
            {field_name: "Quantity must be finite."}
        )

    if quantity < ZERO:
        raise ValidationError(
            {field_name: "Quantity cannot be negative."}
        )

    decimal_places = max(-quantity.as_tuple().exponent, 0)

    if decimal_places > 3:
        raise ValidationError(
            {field_name: "Maximum 3 decimal places are allowed."}
        )

    whole_digits = max(quantity.adjusted() + 1, 0) if quantity else 0

    if whole_digits > 11:
        raise ValidationError(
            {field_name: "Quantity exceeds the maximum allowed value."}
        )

    return quantity.quantize(Decimal("0.001"))


def _determine_review_decision(total_accepted, total_rejected):
    if total_rejected == ZERO:
        return StockTransferReturnReview.Decision.ACCEPTED

    if total_accepted == ZERO:
        return StockTransferReturnReview.Decision.REJECTED

    return StockTransferReturnReview.Decision.PARTIALLY_ACCEPTED


@transaction.atomic
def review_stock_transfer_return(
    *,
    user,
    receipt_id,
    decision,
    items,
    remarks="",
):
    """
    Record the Head Office inspection result for a received
    damaged-stock return batch.

    Review is an immutable audit event.

    IMPORTANT:
    This service does not change stock balances or create
    stock movements.
    """

    # --------------------------------------------------
    # INPUT VALIDATION
    # --------------------------------------------------

    if (
        isinstance(receipt_id, bool)
        or not isinstance(receipt_id, int)
        or receipt_id <= 0
    ):
        raise ValidationError(
            {"receipt_id": "A valid receipt ID is required."}
        )

    if not isinstance(items, (list, tuple)) or not items:
        raise ValidationError(
            {"items": "At least one review item is required."}
        )

    if not isinstance(decision, str):
        raise ValidationError(
            {"decision": "A valid review decision is required."}
        )

    if decision not in StockTransferReturnReview.Decision.values:
        raise ValidationError(
            {"decision": "Invalid return review decision."}
        )

    if not isinstance(remarks, str):
        raise ValidationError(
            {"remarks": "Remarks must be a string."}
        )

    remarks = remarks.strip()

    # --------------------------------------------------
    # LOCK RECEIPT
    # --------------------------------------------------

    receipt = (
        StockTransferReturnReceipt.objects
        .select_for_update()
        .filter(pk=receipt_id)
        .first()
    )

    if receipt is None:
        raise ValidationError(
            {"receipt_id": "Return receipt does not exist."}
        )

    # One review per receipt.
    if StockTransferReturnReview.objects.filter(
        return_receipt=receipt
    ).exists():
        raise ValidationError(
            {"receipt_id": "This return receipt has already been reviewed."}
        )

    # --------------------------------------------------
    # LOCK TRANSFER AND BUSINESSES
    # --------------------------------------------------

    transfer = (
        StockTransfer.objects
        .select_for_update()
        .filter(pk=receipt.return_batch.transfer_id)
        .first()
    )

    if transfer is None:
        raise ValidationError(
            {"receipt_id": "Related stock transfer does not exist."}
        )

    business_ids = sorted(
        [
            transfer.source_business_id,
            transfer.destination_business_id,
        ]
    )

    locked_businesses = list(
        Business.objects
        .select_for_update()
        .filter(pk__in=business_ids)
        .order_by("pk")
    )

    businesses_by_id = {
        business.pk: business
        for business in locked_businesses
    }

    head_office = businesses_by_id.get(
        transfer.source_business_id
    )
    branch = businesses_by_id.get(
        transfer.destination_business_id
    )

    if head_office is None or branch is None:
        raise ValidationError(
            {"receipt_id": "Transfer business information is invalid."}
        )

    if (
        head_office.business_type != Business.BusinessType.HEAD_OFFICE
        or branch.business_type != Business.BusinessType.BRANCH
        or branch.parent_business_id != head_office.pk
        or branch.company_id != head_office.company_id
    ):
        raise ValidationError(
            {"receipt_id": "Invalid Head Office to Branch transfer."}
        )

    validate_business_available(head_office)
    validate_business_available(branch)

    validate_inventory_access(
        user,
        head_office,
        write=True,
    )

    # --------------------------------------------------
    # LOCK RECEIPT ITEMS
    # --------------------------------------------------

    receipt_items = list(
        StockTransferReturnReceiptItem.objects
        .select_for_update()
        .filter(receipt=receipt)
        .order_by("pk")
    )

    if not receipt_items:
        raise ValidationError(
            {"items": "The return receipt has no items to review."}
        )

    receipt_items_by_id = {
        receipt_item.pk: receipt_item
        for receipt_item in receipt_items
    }

    # --------------------------------------------------
    # VALIDATE REVIEW ITEMS
    # --------------------------------------------------

    submitted_items = {}
    total_accepted = ZERO
    total_rejected = ZERO

    for index, item_data in enumerate(items):
        if not isinstance(item_data, dict):
            raise ValidationError(
                {"items": f"Item at index {index} must be an object."}
            )

        receipt_item_id = item_data.get("receipt_item_id")

        if (
            isinstance(receipt_item_id, bool)
            or not isinstance(receipt_item_id, int)
            or receipt_item_id <= 0
        ):
            raise ValidationError(
                {
                    "items": (
                        f"Invalid receipt_item_id at index {index}."
                    )
                }
            )

        if receipt_item_id in submitted_items:
            raise ValidationError(
                {
                    "items": (
                        f"Duplicate receipt item ID: {receipt_item_id}."
                    )
                }
            )

        receipt_item = receipt_items_by_id.get(receipt_item_id)

        if receipt_item is None:
            raise ValidationError(
                {
                    "items": (
                        f"Receipt item {receipt_item_id} "
                        "does not belong to this receipt."
                    )
                }
            )

        accepted_quantity = _validate_review_quantity(
            item_data.get("accepted_quantity"),
            f"items[{index}].accepted_quantity",
        )

        rejected_quantity = _validate_review_quantity(
            item_data.get("rejected_quantity"),
            f"items[{index}].rejected_quantity",
        )

        item_remarks = item_data.get("remarks", "")

        if not isinstance(item_remarks, str):
            raise ValidationError(
                {
                    f"items[{index}].remarks": (
                        "Item remarks must be a string."
                    )
                }
            )

        received_quantity = receipt_item.received_quantity

        if accepted_quantity + rejected_quantity != received_quantity:
            raise ValidationError(
                {
                    "items": (
                        f"Accepted and rejected quantities for receipt "
                        f"item {receipt_item_id} must equal its received "
                        f"quantity ({received_quantity})."
                    )
                }
            )

        submitted_items[receipt_item_id] = {
            "receipt_item": receipt_item,
            "accepted_quantity": accepted_quantity,
            "rejected_quantity": rejected_quantity,
            "remarks": item_remarks.strip(),
        }

        total_accepted += accepted_quantity
        total_rejected += rejected_quantity

    # Every receipt item must be reviewed.
    if set(submitted_items) != set(receipt_items_by_id):
        raise ValidationError(
            {
                "items": (
                    "Every return receipt item must be included "
                    "in the review."
                )
            }
        )

    # --------------------------------------------------
    # VALIDATE OVERALL DECISION
    # --------------------------------------------------

    calculated_decision = _determine_review_decision(
        total_accepted,
        total_rejected,
    )

    if decision != calculated_decision:
        raise ValidationError(
            {
                "decision": (
                    f"Decision must be "
                    f"'{calculated_decision}' based on the "
                    "accepted and rejected quantities."
                )
            }
        )

    # --------------------------------------------------
    # CREATE IMMUTABLE REVIEW EVENT
    # --------------------------------------------------

    review = StockTransferReturnReview.objects.create(
        return_receipt=receipt,
        decision=decision,
        remarks=remarks,
        reviewed_by=user,
    )

    # --------------------------------------------------
    # CREATE REVIEW ITEMS
    # --------------------------------------------------

    for item_data in submitted_items.values():
        StockTransferReturnReviewItem.objects.create(
            review=review,
            receipt_item=item_data["receipt_item"],
            accepted_quantity=item_data["accepted_quantity"],
            rejected_quantity=item_data["rejected_quantity"],
            remarks=item_data["remarks"],
        )

    # --------------------------------------------------
    # IMPORTANT
    # --------------------------------------------------
    # Review is an inspection/audit event only.
    # No StockBalance update.
    # No StockMovement creation.

    return review
