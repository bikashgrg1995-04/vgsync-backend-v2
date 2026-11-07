
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.businesses.models import Business
from apps.inventory.models import Product, StockBalance, StockMovement
from apps.inventory.services.validators import (
    validate_inventory_access,
    validate_product_business,
)


# ==================================================
# GENERIC STOCK MOVEMENT TYPES
# ==================================================

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


# ==================================================
# STOCK ADJUSTMENT REASONS
# ==================================================

ADJUSTMENT_REASONS = {
    "increase": {
        "received",
        "found",
        "count_correction",
        "other",
    },
    "decrease": {
        "damaged",
        "lost",
        "expired",
        "used",
        "count_correction",
        "other",
    },
    "remove_damaged": {
        "disposed",
        "written_off",
        "other",
    },
}

ADJUSTMENT_REASON_LABELS = {
    "received": "Additional stock received",
    "found": "Previously missing stock found",
    "count_correction": "Stock count correction",
    "damaged": "Reclassified as damaged",
    "lost": "Lost or missing",
    "expired": "Expired stock",
    "used": "Used stock",
    "disposed": "Damaged stock disposed",
    "written_off": "Damaged stock written off",
    "other": "Other adjustment",
}


# ==================================================
# QUANTITY VALIDATION
# ==================================================

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


# ==================================================
# STOCK CONDITION VALIDATION
# ==================================================

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


# ==================================================
# GENERIC STOCK MOVEMENT
# ==================================================

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


# ==================================================
# STOCK ADJUSTMENT
# ==================================================

@transaction.atomic
def create_stock_adjustment(
    *,
    user,
    business,
    product,
    action,
    quantity,
    reason,
    reference="",
    notes="",
):
    """
    Apply a manual stock adjustment atomically.

    Actions:

    1. increase
       Increase good stock.

    2. decrease
       Decrease good stock.
       If reason is 'damaged', reclassify good stock
       into damaged stock.

    3. remove_damaged
       Remove existing damaged stock.

    Damaged reclassification creates two immutable
    stock movement records within one transaction.
    """

    # --------------------------------------------------
    # VALIDATE ACTION AND REASON
    # --------------------------------------------------

    if action not in ADJUSTMENT_REASONS:
        raise ValidationError({
            "action": "Invalid stock adjustment action."
        })

    if reason not in ADJUSTMENT_REASONS[action]:
        raise ValidationError({
            "reason": "Invalid reason for this adjustment action."
        })

    quantity = validate_quantity(quantity)

    reference = reference or ""
    notes = (notes or "").strip()

    # --------------------------------------------------
    # LOCK AND VALIDATE BUSINESS AND PRODUCT
    # --------------------------------------------------

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

    # --------------------------------------------------
    # GET OR CREATE STOCK BALANCE
    # --------------------------------------------------

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

    # --------------------------------------------------
    # PREPARE MOVEMENT NOTES
    # --------------------------------------------------

    reason_label = ADJUSTMENT_REASON_LABELS[reason]

    movement_notes = f"Reason: {reason_label}"

    if notes:
        movement_notes += f"\n{notes}"

    # --------------------------------------------------
    # ACTION: INCREASE GOOD STOCK
    # --------------------------------------------------

    if action == "increase":

        before = balance.quantity
        after = before + quantity

        balance.quantity = after

        balance.save(
            update_fields=[
                "quantity",
                "updated_at",
            ]
        )

        return StockMovement.objects.create(
            stock_balance=balance,
            stock_condition=StockMovement.StockCondition.GOOD,
            movement_type=StockMovement.MovementType.ADJUSTMENT_IN,
            quantity=quantity,
            balance_before=before,
            balance_after=after,
            reference=reference,
            notes=movement_notes,
            created_by=user,
        )

    # --------------------------------------------------
    # ACTION: REMOVE DAMAGED STOCK
    # --------------------------------------------------

    if action == "remove_damaged":

        before = balance.damaged_quantity

        if before < quantity:
            raise ValidationError({
                "quantity": (
                    f"Insufficient damaged stock. "
                    f"Available: {before}"
                )
            })

        after = before - quantity

        balance.damaged_quantity = after

        balance.save(
            update_fields=[
                "damaged_quantity",
                "updated_at",
            ]
        )

        return StockMovement.objects.create(
            stock_balance=balance,
            stock_condition=StockMovement.StockCondition.DAMAGED,
            movement_type=StockMovement.MovementType.ADJUSTMENT_OUT,
            quantity=quantity,
            balance_before=before,
            balance_after=after,
            reference=reference,
            notes=movement_notes,
            created_by=user,
        )

    # --------------------------------------------------
    # ACTION: DECREASE GOOD STOCK
    # --------------------------------------------------

    good_before = balance.quantity

    if good_before < quantity:
        raise ValidationError({
            "quantity": (
                f"Insufficient good stock. "
                f"Available: {good_before}"
            )
        })

    good_after = good_before - quantity

    # --------------------------------------------------
    # REASON: DAMAGED
    #
    # Reclassify good stock into damaged stock.
    # Both movements are part of the same transaction.
    # --------------------------------------------------

    if reason == "damaged":

        damaged_before = balance.damaged_quantity
        damaged_after = damaged_before + quantity

        # Update both balances together.
        balance.quantity = good_after
        balance.damaged_quantity = damaged_after

        balance.save(
            update_fields=[
                "quantity",
                "damaged_quantity",
                "updated_at",
            ]
        )

        # Movement 1: Remove quantity from good stock.
        good_movement = StockMovement.objects.create(
            stock_balance=balance,
            stock_condition=StockMovement.StockCondition.GOOD,
            movement_type=StockMovement.MovementType.ADJUSTMENT_OUT,
            quantity=quantity,
            balance_before=good_before,
            balance_after=good_after,
            reference=reference,
            notes=movement_notes,
            created_by=user,
        )

        # Movement 2: Add the same quantity to damaged stock.
        StockMovement.objects.create(
            stock_balance=balance,
            stock_condition=StockMovement.StockCondition.DAMAGED,
            movement_type=StockMovement.MovementType.ADJUSTMENT_IN,
            quantity=quantity,
            balance_before=damaged_before,
            balance_after=damaged_after,
            reference=reference,
            notes=movement_notes,
            created_by=user,
        )

        return good_movement

    # --------------------------------------------------
    # NORMAL DECREASE
    #
    # Lost, expired, used, count correction or other.
    # --------------------------------------------------

    balance.quantity = good_after

    balance.save(
        update_fields=[
            "quantity",
            "updated_at",
        ]
    )

    return StockMovement.objects.create(
        stock_balance=balance,
        stock_condition=StockMovement.StockCondition.GOOD,
        movement_type=StockMovement.MovementType.ADJUSTMENT_OUT,
        quantity=quantity,
        balance_before=good_before,
        balance_after=good_after,
        reference=reference,
        notes=movement_notes,
        created_by=user,
    )
