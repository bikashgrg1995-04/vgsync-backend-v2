
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.businesses.models import Business
from apps.inventory.models import (
    StockBalance,
    StockMovement,
    StockTransfer,
    StockTransferReturn,
    StockTransferReturnReceipt,
    StockTransferReturnReceiptItem,
)
from apps.inventory.services.stock import validate_quantity
from apps.inventory.services.validators import (
    validate_business_available,
    validate_inventory_access,
)


def receive_stock_transfer_return(
    *,
    user,
    return_batch_id,
    items,
    reference,
    notes="",
):
    """
    Record the complete physical receipt of one Branch damaged-stock
    return batch at its Head Office.

    One return batch can have only one complete HO receipt.
    Receipt does not change Branch stock.
    """

    with transaction.atomic():

        # --------------------------------------------------
        # VALIDATE INPUT
        # --------------------------------------------------

        if (
            not isinstance(return_batch_id, int)
            or isinstance(return_batch_id, bool)
            or return_batch_id <= 0
        ):
            raise ValidationError("Invalid return batch ID.")

        if not isinstance(items, (list, tuple)) or not items:
            raise ValidationError(
                "At least one return receipt item is required."
            )

        if not isinstance(reference, str) or not reference.strip():
            raise ValidationError("Receipt reference is required.")

        reference = reference.strip()

        if len(reference) > 100:
            raise ValidationError(
                "Receipt reference cannot exceed 100 characters."
            )

        if not isinstance(notes, str):
            raise ValidationError("Notes must be text.")

        # --------------------------------------------------
        # LOCK RETURN BATCH
        # --------------------------------------------------

        return_batch = (
            StockTransferReturn.objects
            .select_for_update()
            .filter(pk=return_batch_id)
            .first()
        )

        if return_batch is None:
            raise ValidationError("Return batch was not found.")

        # A batch can receive only one complete HO receipt.
        if StockTransferReturnReceipt.objects.filter(
            return_batch=return_batch
        ).exists():
            raise ValidationError(
                "This return batch has already been received "
                "by the Head Office."
            )

        # --------------------------------------------------
        # LOCK TRANSFER
        # --------------------------------------------------

        transfer = (
            StockTransfer.objects
            .select_for_update()
            .filter(pk=return_batch.transfer_id)
            .first()
        )

        if transfer is None:
            raise ValidationError(
                "The original stock transfer was not found."
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
            or branch.business_type
            != Business.BusinessType.BRANCH
            or branch.parent_business_id != head_office.pk
            or branch.company_id != head_office.company_id
        ):
            raise ValidationError(
                "Invalid Head Office to Branch transfer."
            )

        validate_business_available(head_office)
        validate_business_available(branch)

        # --------------------------------------------------
        # ACCESS VALIDATION
        # --------------------------------------------------

        validate_inventory_access(
            user,
            head_office,
            write=True,
        )

        # --------------------------------------------------
        # LOCK ALL RETURN ITEMS
        # --------------------------------------------------

        return_items = list(
            return_batch.items
            .select_for_update()
            .select_related(
                "transfer_item",
                "transfer_item__product",
            )
            .order_by("pk")
        )

        if not return_items:
            raise ValidationError(
                "This return batch has no return items."
            )

        return_items_by_id = {
            item.pk: item
            for item in return_items
        }

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

            return_item_id = item.get("return_item_id")

            if (
                not isinstance(return_item_id, int)
                or isinstance(return_item_id, bool)
                or return_item_id <= 0
            ):
                raise ValidationError(
                    "Invalid return item ID."
                )

            if return_item_id in seen_item_ids:
                raise ValidationError(
                    f"Duplicate return item ID: {return_item_id}."
                )

            seen_item_ids.add(return_item_id)

            return_item = return_items_by_id.get(return_item_id)

            if return_item is None:
                raise ValidationError(
                    f"Return item {return_item_id} "
                    "does not belong to this return batch."
                )

            received_quantity = validate_quantity(
                item.get("received_quantity")
            )

            # This is a complete receipt, not a partial receipt.
            if received_quantity != return_item.quantity:
                raise ValidationError(
                    f"Head Office must receive the complete returned "
                    f"quantity for "
                    f"{return_item.transfer_item.product.name}. "
                    f"Expected: {return_item.quantity}, "
                    f"received: {received_quantity}."
                )

            product = return_item.transfer_item.product

            if product.owner_business_id != head_office.pk:
                raise ValidationError(
                    "Return product does not belong to this "
                    "Head Office."
                )

            prepared_items.append(
                (return_item, received_quantity)
            )

        # Every item in the return batch must be included.
        if seen_item_ids != set(return_items_by_id):
            missing_ids = sorted(
                set(return_items_by_id) - seen_item_ids
            )

            raise ValidationError(
                "A complete receipt must include every return item. "
                f"Missing return item IDs: {missing_ids}."
            )

        # --------------------------------------------------
        # LOCK HEAD OFFICE STOCK BALANCES
        # --------------------------------------------------

        product_ids = sorted({
            return_item.transfer_item.product_id
            for return_item, _ in prepared_items
        })

        balances = {
            balance.product_id: balance
            for balance in (
                StockBalance.objects
                .select_for_update()
                .filter(
                    business=head_office,
                    product_id__in=product_ids,
                )
                .order_by("product_id")
            )
        }

        for product_id in product_ids:
            if product_id not in balances:
                raise ValidationError(
                    "Head Office stock balance was not found "
                    "for a returned product."
                )

        # --------------------------------------------------
        # CREATE COMPLETE HO RECEIPT
        # --------------------------------------------------

        receipt = StockTransferReturnReceipt.objects.create(
            return_batch=return_batch,
            reference=reference,
            notes=notes.strip(),
            received_by=user,
        )

        # --------------------------------------------------
        # INCREASE HO DAMAGED STOCK AND CREATE HISTORY
        # --------------------------------------------------

        for return_item, quantity in prepared_items:

            product = return_item.transfer_item.product
            balance = balances[product.pk]

            balance_before = balance.damaged_quantity
            balance_after = balance_before + quantity

            receipt_item = StockTransferReturnReceiptItem.objects.create(
                receipt=receipt,
                return_item=return_item,
                received_quantity=quantity,
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
                movement_type=StockMovement.MovementType.TRANSFER_IN,
                quantity=quantity,
                balance_before=balance_before,
                balance_after=balance_after,
                transfer_return_receipt_item=receipt_item,
                reference=reference,
                notes=(
                    f"Damaged stock return received: "
                    f"{transfer.reference}"
                ),
                created_by=user,
            )

        return receipt
