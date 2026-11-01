
from django.core.exceptions import PermissionDenied, ValidationError

from apps.accounts.access import (
    ACCESS_FULL,
    AccessDenied,
    ensure_business_available,
    validate_user_access,
)
from apps.businesses.models import Business


def validate_inventory_access(user, business, *, write=False):
    """
    Validate that the user can access the requested business.

    Write operations require full business access.
    Module-level permissions are checked separately by API permissions.
    """
    try:
        access_mode = validate_user_access(user, business)
    except AccessDenied as exc:
        raise PermissionDenied(exc.message) from exc

    if write and access_mode != ACCESS_FULL:
        raise PermissionDenied(
            "You have read-only access to this business."
        )

    return access_mode


def validate_business_available(business):
    """Ensure the business and its company are active."""
    try:
        ensure_business_available(business)
    except AccessDenied as exc:
        raise ValidationError(exc.message) from exc


def validate_product_business(product, business, *, require_active=True):
    """
    Ensure a product can be used by the requested business.

    Independent Business:
        Can only use its own products.

    Head Office:
        Can use its own products.

    Branch:
        Can use products owned by its direct Head Office.
    """
    validate_business_available(business)

    owner = product.owner_business
    validate_business_available(owner)

    if require_active and not product.is_active:
        raise ValidationError("This product is inactive.")

    if owner.business_type == Business.BusinessType.INDEPENDENT:
        if business.pk != owner.pk:
            raise ValidationError(
                "Independent business products cannot be shared "
                "with another business."
            )
        return True

    if owner.business_type == Business.BusinessType.HEAD_OFFICE:
        if business.pk == owner.pk:
            return True

        is_valid_branch = (
            business.business_type == Business.BusinessType.BRANCH
            and business.parent_business_id == owner.pk
            and business.company_id == owner.company_id
        )

        if not is_valid_branch:
            raise ValidationError(
                "This product does not belong to your "
                "Head Office or its branches."
            )
        return True

    raise ValidationError("Invalid product owner business type.")
