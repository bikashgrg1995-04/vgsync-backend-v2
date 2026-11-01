
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.businesses.models import Business
from apps.inventory.models import Product, StockBalance, StockMovement
from apps.inventory.services.validators import (
    validate_inventory_access,
    validate_product_business,
)


INCREASE_TYPES = {
    StockMovement.MovementType.OPENING,
    StockMovement.MovementType.PURCHASE_RECEIPT,
    StockMovement.MovementType.SALE_RETURN,
    StockMovement.MovementType.ADJUSTMENT_IN,
}

DECREASE_TYPES = {
    StockMovement.MovementType.SALE_ISSUE,
    StockMovement.MovementType.PURCHASE_RETURN,
    StockMovement.MovementType.ADJUSTMENT_OUT,
}

ALLOWED_TYPES = INCREASE_TYPES | DECREASE_TYPES


def validate_quantity(value):
    """
    Accept only Decimal or decimal strings.
    Reject floats, integers, booleans and invalid precision.
    """

    if isinstance(value, bool) or not isinstance(value, (Decimal, str)):
        raise ValidationError(
            "Quantity must be a Decimal or a decimal string."
        )

    try:
        quantity = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValidationError("Invalid quantity.")

    if not quantity.is_finite():
        raise ValidationError(
            "Quantity must be a finite number."
        )

    if quantity <= 0:
        raise ValidationError(
            "Quantity must be greater than zero."
        )

    if quantity.as_tuple().exponent < -3:
        raise ValidationError(
            "Quantity cannot have more than 3 decimal places."
        )

    field = StockBalance._meta.get_field("quantity")

    try:
        field.clean(quantity, None)
    except ValidationError:
        raise ValidationError(
            "Quantity exceeds the allowed limit."
        )

    return quantity


def validate_stock_condition(stock_condition):
    """
    Validate whether the movement affects good or damaged stock.
    """

    valid_conditions = {
        StockMovement.StockCondition.GOOD,
        StockMovement.StockCondition.DAMAGED,
    }

    if stock_condition not in valid_conditions:
        raise ValidationError(
            "Invalid stock condition."
        )

    return stock_condition


@transaction.atomic
def create_stock_movement(
    *,
    user,
    business,
    product,
    movement_type,
    quantity,
    stock_condition=StockMovement.StockCondition.GOOD,
    reference="",
    notes="",
):
    """
    Apply one stock movement atomically.

    Generic stock movements support:
    - Opening
    - Purchase receipt
    - Sale issue
    - Sale return
    - Purchase return
    - Adjustment increase/decrease

    Transfers and reversals must use dedicated services.

    Stock condition determines which StockBalance quantity
    field is updated.
    """

    if movement_type not in ALLOWED_TYPES:
        raise ValidationError(
            "Unsupported stock movement type. "
            "Transfers and reversals must use dedicated services."
        )

    quantity = validate_quantity(quantity)
    stock_condition = validate_stock_condition(stock_condition)

    # Lock business first, then product.
    # This serializes balance creation for the same product/business.
    locked_business = (
        Business.objects.select_for_update()
        .get(pk=business.pk)
    )

    locked_product = (
        Product.objects.select_for_update()
        .select_related("owner_business")
        .get(pk=product.pk)
    )

    validate_inventory_access(
        user,
        locked_business,
        write=True,
    )

    validate_product_business(
        locked_product,
        locked_business,
    )

    balance, _ = StockBalance.objects.get_or_create(
        product=locked_product,
        business=locked_business,
        defaults={
            "quantity": Decimal("0.000"),
            "damaged_quantity": Decimal("0.000"),
        },
    )

    balance = (
        StockBalance.objects.select_for_update()
        .get(pk=balance.pk)
    )

    # Determine which stock quantity to update.
    balance_field = (
        "quantity"
        if stock_condition == StockMovement.StockCondition.GOOD
        else "damaged_quantity"
    )

    before = getattr(balance, balance_field)

    if movement_type in INCREASE_TYPES:
        after = before + quantity

    else:
        if before < quantity:
            raise ValidationError(
                f"Insufficient {stock_condition} stock. "
                f"Available: {before}"
            )

        after = before - quantity

    setattr(balance, balance_field, after)

    balance.save(
        update_fields=[balance_field, "updated_at"]
    )

    movement = StockMovement.objects.create(
        stock_balance=balance,
        stock_condition=stock_condition,
        movement_type=movement_type,
        quantity=quantity,
        balance_before=before,
        balance_after=after,
        reference=reference,
        notes=notes,
        created_by=user,
    )

    return movement
