from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.inventory.models import StockTransfer
from apps.inventory.services.validators import validate_inventory_access


@transaction.atomic
def cancel_stock_transfer_order(*, user, business, transfer_id, reason):
    """
    Cancel an order that Head Office has not started to dispatch.

    The Branch that placed it can withdraw it, and Head Office can turn it
    down. Nothing has moved yet, so no stock changes. The reason is kept in
    the order's notes.
    """

    reason = (reason or "").strip() if isinstance(reason, str) else ""

    if not reason:
        raise ValidationError({"reason": "A reason is required."})

    transfer = (
        StockTransfer.objects
        .select_for_update()
        .filter(pk=transfer_id)
        .first()
    )

    if transfer is None or business.pk not in (
        transfer.source_business_id,
        transfer.destination_business_id,
    ):
        raise ValidationError("Order not found.")

    validate_inventory_access(user, business, write=True)

    if transfer.status != StockTransfer.Status.ORDERED:
        raise ValidationError(
            "Only an order that has not been dispatched can be cancelled."
        )

    side = (
        "Branch" if business.pk == transfer.destination_business_id
        else "Head Office"
    )
    line = f"Cancelled by {side} on {timezone.localdate()}: {reason}"

    transfer.status = StockTransfer.Status.CANCELLED
    transfer.notes = f"{transfer.notes}\n{line}".strip()
    transfer.save(update_fields=["status", "notes", "updated_at"])

    return transfer
