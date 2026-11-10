
from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.accounts import rbac
from apps.accounts.models import User


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


_METHOD_ACTIONS = {
    "GET": "view",
    "HEAD": "view",
    "POST": "create",
    "PUT": "update",
    "PATCH": "update",
    "DELETE": "delete",
}


def required_action(request, view):
    """
    The action a request needs: from the method (GET view, POST create, ...)
    unless the view maps one of its own actions in ``permission_actions``,
    for example ``{"cancel": "cancel", "void": "void"}``.
    """
    mapped = getattr(view, "permission_actions", {}).get(
        getattr(view, "action", None)
    )

    return mapped or _METHOD_ACTIONS.get(request.method)


class HasModulePermission(BasePermission):
    """
    Module permission check backed by the permission resolver
    (``apps.accounts.rbac``): owner, else Position grants with the person's
    own overrides. The answer is read from the database on every request, so
    a change applies immediately.

    Views using this permission must define ``permission_module`` and may
    define ``permission_actions`` for non-CRUD actions.
    """

    message = "You do not have permission to perform this action."

    def has_permission(self, request, view):
        user = request.user
        business = getattr(request, "active_business", None)

        if not user or not user.is_authenticated or business is None:
            return False

        # OPTIONS is required for API metadata/preflight handling.
        if request.method == "OPTIONS":
            return True

        action = required_action(request, view)
        module = getattr(view, "permission_module", None)

        # Unknown methods and views without a module are denied.
        if not action or not module:
            return False

        return rbac.has_permission(
            user,
            f"{module}.{action}",
            getattr(request, "access_mode", "full"),
        )


class CanManageTeam(BasePermission):
    """
    Who may use the team (staff) API: the owner, and staff holding the
    matching ``staff.*`` permission. Owners follow ``IsOwner`` (read-only
    for a company owner inside a Branch).
    """

    message = "You do not have permission to manage the team."

    def has_permission(self, request, view):
        user = request.user
        business = getattr(request, "active_business", None)

        if not user or not user.is_authenticated or business is None:
            return False

        if request.method == "OPTIONS":
            return True

        if user.role == User.Role.OWNER:
            return IsOwner().has_permission(request, view)

        action = required_action(request, view)

        if not action:
            return False

        return rbac.has_permission(
            user,
            f"staff.{action}",
            getattr(request, "access_mode", "full"),
        )


class HasPermission(BasePermission):
    """
    Needs one explicit code, set on the view as ``required_permission`` (or
    per action in ``required_permissions``). For endpoints that are not a
    plain module (reports, approvals).
    """

    message = "You do not have permission to perform this action."

    def has_permission(self, request, view):
        user = request.user
        business = getattr(request, "active_business", None)

        if not user or not user.is_authenticated or business is None:
            return False

        if request.method == "OPTIONS":
            return True

        per_action = getattr(view, "required_permissions", {})
        code = per_action.get(getattr(view, "action", None)) or getattr(
            view, "required_permission", None
        )

        if not code:
            return False

        return rbac.has_permission(
            user, code, getattr(request, "access_mode", "full")
        )
