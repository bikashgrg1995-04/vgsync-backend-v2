
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.businesses.models import Business
from apps.inventory.models import Product
from apps.inventory.services.validators import (
    validate_business_available,
    validate_inventory_access,
)


def get_products_for_business(business):
    """
    Return the catalog visible to the active business.

    Head Office:
        Own central catalog.

    Branch:
        Parent Head Office catalog.

    Independent:
        Own catalog.
    """
    validate_business_available(business)

    if business.business_type == Business.BusinessType.BRANCH:
        if not business.parent_business_id:
            raise ValidationError(
                "This branch does not have a Head Office."
            )

        owner_business_id = business.parent_business_id

    elif business.business_type in (
        Business.BusinessType.HEAD_OFFICE,
        Business.BusinessType.INDEPENDENT,
    ):
        owner_business_id = business.pk

    else:
        raise ValidationError("Unsupported business type.")

    return (
        Product.objects
        .filter(
            owner_business_id=owner_business_id,
            is_active=True,
        )
        .select_related("owner_business")
        .order_by("name")
    )


def get_product_for_business(*, business, product_id):
    """
    Retrieve a product only if it belongs to the active
    business's visible catalog.
    """
    return get_products_for_business(business).filter(
        pk=product_id
    ).first()


def validate_product_write_access(user, business):
    """
    Only Head Office and Independent Business can manage
    their own product catalogs.
    """
    validate_business_available(business)
    validate_inventory_access(user, business, write=True)

    if business.business_type == Business.BusinessType.BRANCH:
        raise PermissionDenied(
            "Branches have read-only access to the product catalog."
        )

    if business.business_type not in (
        Business.BusinessType.HEAD_OFFICE,
        Business.BusinessType.INDEPENDENT,
    ):
        raise PermissionDenied(
            "This business cannot manage a product catalog."
        )


@transaction.atomic
def create_product(*, user, business, validated_data):
    validate_product_write_access(user, business)

    product = Product(
        owner_business=business,
        **validated_data,
    )

    product.full_clean()
    product.save()

    return product


@transaction.atomic
def update_product(*, user, business, product_id, validated_data):
    validate_product_write_access(user, business)

    product = (
        Product.objects
        .select_for_update()
        .filter(
            pk=product_id,
            owner_business=business,
            is_active=True,
        )
        .first()
    )

    if not product:
        raise ValidationError("Product not found.")

    for field, value in validated_data.items():
        setattr(product, field, value)

    product.full_clean()
    product.save()

    return product


@transaction.atomic
def deactivate_product(*, user, business, product_id):
    validate_product_write_access(user, business)

    product = (
        Product.objects
        .select_for_update()
        .filter(
            pk=product_id,
            owner_business=business,
            is_active=True,
        )
        .first()
    )

    if not product:
        raise ValidationError("Product not found.")

    product.is_active = False
    product.save(update_fields=["is_active", "updated_at"])

    return product
