
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.businesses.models import Business
from apps.inventory.models import (
    Product,
    StockTransfer,
    StockTransferItem,
)
from apps.inventory.services.stock import validate_quantity
from apps.inventory.services.validators import (
    validate_inventory_access,
    validate_product_business,
)


@transaction.atomic
def create_stock_transfer_order(
    *,
    user,
    branch,
    items,
    reference,
    notes="",
):
    """
    Create a Branch-to-Head-Office stock transfer order.

    No stock is changed when an order is created.
    """

    if not isinstance(items, (list, tuple)) or not items:
        raise ValidationError("At least one product is required.")

    if not isinstance(reference, str) or not reference.strip():
        raise ValidationError("Reference is required.")

    # Lock the branch and its Head Office.
    locked_branch = (
        Business.objects.select_for_update()
        .get(pk=branch.pk)
    )

    if locked_branch.business_type != Business.BusinessType.BRANCH:
        raise ValidationError(
            "Only a Branch can create a stock transfer order."
        )

    if not locked_branch.parent_business_id:
        raise ValidationError("Branch has no Head Office.")

    locked_head_office = (
        Business.objects.select_for_update()
        .get(pk=locked_branch.parent_business_id)
    )

    if (
        locked_head_office.business_type
        != Business.BusinessType.HEAD_OFFICE
        or locked_head_office.company_id != locked_branch.company_id
    ):
        raise ValidationError("Invalid Head Office for this Branch.")

    validate_inventory_access(user, locked_branch, write=True)

    if not locked_branch.is_active or not locked_head_office.is_active:
        raise ValidationError("Branch or Head Office is inactive.")

    prepared_items = []
    seen_product_ids = set()

    for item in items:
        if not isinstance(item, dict):
            raise ValidationError("Each order item must be an object.")

        product_id = item.get("product_id")
        if isinstance(product_id, bool) or not isinstance(product_id, int):
            raise ValidationError("Invalid product ID.")

        if product_id in seen_product_ids:
            raise ValidationError(
                "A product cannot appear more than once in an order."
            )

        seen_product_ids.add(product_id)

        quantity = validate_quantity(item.get("quantity"))

        product = (
            Product.objects.select_for_update()
            .select_related("owner_business")
            .filter(pk=product_id)
            .first()
        )

        if product is None:
            raise ValidationError(
                f"Product with ID {product_id} does not exist."
            )

        validate_product_business(
            product,
            locked_branch,
            require_active=True,
        )

        if product.owner_business_id != locked_head_office.pk:
            raise ValidationError(
                "Order items must belong to this Branch's Head Office."
            )

        prepared_items.append((product, quantity))

    transfer = StockTransfer.objects.create(
        source_business=locked_head_office,
        destination_business=locked_branch,
        reference=reference.strip(),
        notes=notes.strip() if isinstance(notes, str) else "",
        created_by=user,
    )

    StockTransferItem.objects.bulk_create([
        StockTransferItem(
            transfer=transfer,
            product=product,
            ordered_quantity=quantity,
        )
        for product, quantity in prepared_items
    ])

    return transfer
