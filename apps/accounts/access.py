"""
Single source of truth for "may this user act inside this business?".

Login, token refresh, request authentication and the current-user endpoint
all call ``validate_user_access`` so the tenant, role and Position rules
cannot drift apart. Callers translate ``AccessDenied`` into whatever error
type fits their layer (validation error, invalid token, 401/403 response).
"""

from apps.businesses.models import Business

from .models import User

ACCESS_FULL = "full"
ACCESS_READ_ONLY = "read_only"

# Roles that apply across a company (Head Office full access, branches read-only).
COMPANY_WIDE_ROLES = (User.Role.OWNER,)

# Roles that can belong directly to a business.
BUSINESS_ROLES = COMPANY_WIDE_ROLES + (User.Role.STAFF,)


class AccessDenied(Exception):
    """The user is not allowed to act inside the given business."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def ensure_business_available(business):
    """The business, and its company if any, must exist and be active."""
    if business is None or not business.is_active:
        raise AccessDenied("The selected business is unavailable.")

    if business.company_id and not business.company.is_active:
        raise AccessDenied("The selected company is inactive.")


def get_access_mode(user, business):
    """
    Company users get read-only access to branches. Everyone else has
    full access to the business they are allowed into.
    """
    if (
        user.company_id
        and business.business_type == Business.BusinessType.BRANCH
    ):
        return ACCESS_READ_ONLY

    return ACCESS_FULL


def validate_user_access(user, business):
    """
    Check that ``user`` may act inside ``business`` right now, using the
    current database state. Returns the access mode (``"full"`` or
    ``"read_only"``) or raises ``AccessDenied``.

    ``business`` should be loaded with ``select_related("company")`` and the
    user with ``select_related("position")`` to avoid extra queries.
    """
    if not user.is_active or user.role == User.Role.SUPER_ADMIN:
        raise AccessDenied("This account is not allowed to access client APIs.")

    ensure_business_available(business)

    if user.company_id:
        if (
            user.company_id != business.company_id
            or user.tenant_company_id != user.company_id
        ):
            raise AccessDenied("You are not allowed to access this business.")

        if user.role == User.Role.STAFF:
            if business.business_type != Business.BusinessType.HEAD_OFFICE:
                raise AccessDenied("Company Staff can only access Head Office.")

        elif user.role not in COMPANY_WIDE_ROLES:
            raise AccessDenied("Your role is not authorized for this access.")

    elif user.business_id:
        if (
            user.business_id != business.id
            or user.tenant_company_id != business.company_id
        ):
            raise AccessDenied("You are not allowed to access this business.")

        if user.role not in BUSINESS_ROLES:
            raise AccessDenied("Your role is not authorized for this access.")

    else:
        raise AccessDenied("Your account has no valid business assignment.")

    # Staff need an active Position that belongs to the business they are in.
    # (Company Staff are only allowed into Head Office, and business Staff
    # only into their own business, so this is always the active business.)
    if user.role == User.Role.STAFF:
        if not user.position_id or not user.position.is_active:
            raise AccessDenied("Your account does not have an active position.")

        if user.position.business_id != business.id:
            raise AccessDenied("Your assigned position is invalid.")

    return get_access_mode(user, business)


def serialize_active_business(business, access_mode):
    """The ``active_business`` payload returned by login and the /me endpoint."""
    return {
        "id": business.id,
        "name": business.name,
        "business_code": business.business_code,
        "business_type": business.business_type,
        "company_id": business.company_id,
        "company_name": business.company.name if business.company else None,
        "access_mode": access_mode,
    }
