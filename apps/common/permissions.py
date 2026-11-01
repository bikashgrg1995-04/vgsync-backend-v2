
from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.accounts.models import PositionPermission, User


class HasActiveBusinessContext(BasePermission):
    """
    Requires a valid active business context established
    by VGSyncJWTAuthentication.
    """

    message = "A valid active business context is required."

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and getattr(request, "active_business", None) is not None
        )


class IsOwner(BasePermission):
    """
    Only the owner of the active business may use the view.

    A company owner has full access in the Head Office but is read-only
    inside Branches (the same ``access_mode`` rule as everywhere else), so
    writes are refused there.
    """

    message = "Only the owner can manage this."

    def has_permission(self, request, view):
        user = request.user
        business = getattr(request, "active_business", None)

        if not user or not user.is_authenticated or business is None:
            return False

        if user.role != User.Role.OWNER:
            return False

        if request.method in SAFE_METHODS:
            return True

        return getattr(request, "access_mode", None) == "full"


class HasModulePermission(BasePermission):
    """
    Enforces role-based and Position-based module permissions.

    Views using this permission must define:
        permission_module = "inventory"
    """

    message = "You do not have permission to perform this action."

    ACTION_FIELDS = {
        "GET": "can_view",
        "HEAD": "can_view",
        "POST": "can_create",
        "PUT": "can_update",
        "PATCH": "can_update",
        "DELETE": "can_delete",
    }

    def has_permission(self, request, view):
        user = request.user
        business = getattr(request, "active_business", None)

        if not user or not user.is_authenticated or business is None:
            return False

        # OPTIONS is required for API metadata/preflight handling.
        if request.method == "OPTIONS":
            return True

        permission_field = self.ACTION_FIELDS.get(request.method)

        # Unknown HTTP methods are denied.
        if not permission_field:
            return False

        module = getattr(view, "permission_module", None)

        # Fail closed if the API view has no module configured.
        if not module:
            return False

        # Company-level users have full access to Head Office.
        # Their branch access is read-only.
        if user.company_id:
            if user.role == User.Role.STAFF:
                return self._has_position_permission(
                    user,
                    module,
                    permission_field,
                )

            if user.role in [
                User.Role.OWNER,
                User.Role.ADMIN,
                User.Role.ACCOUNTANT,
            ]:
                if getattr(request, "access_mode", None) == "read_only":
                    return permission_field == "can_view"

                return True

            return False

        # Business-level users can access their assigned business.
        if user.business_id:
            if user.role == User.Role.STAFF:
                return self._has_position_permission(
                    user,
                    module,
                    permission_field,
                )

            return user.role in [
                User.Role.OWNER,
                User.Role.ADMIN,
                User.Role.ACCOUNTANT,
            ]

        return False

    @staticmethod
    def _has_position_permission(user, module, permission_field):
        if not user.position_id:
            return False

        position = user.position

        if not position.is_active:
            return False

        return PositionPermission.objects.filter(
            position=position,
            module=module,
            **{permission_field: True},
        ).exists()
