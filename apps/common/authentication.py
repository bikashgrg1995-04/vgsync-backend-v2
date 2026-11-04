
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.audit import context as audit_context
from apps.accounts.access import AccessDenied, validate_user_access
from apps.accounts.models import User
from apps.businesses.models import Business

from .exceptions import PasswordChangeRequired

# Views a user may still call while ``must_change_password`` is set: reading
# their own profile (so the client can see the flag) and changing the
# password. Login, refresh and logout do not use this authentication class.
# Everything else is blocked, so new endpoints are protected by default.
PASSWORD_CHANGE_ALLOWED_VIEWS = frozenset({
    "accounts:current-user",
    "accounts:change-password",
})


class VGSyncJWTAuthentication(JWTAuthentication):

    def authenticate(self, request):
        result = super().authenticate(request)

        if result is None:
            return None

        user, token = result

        # Super Admin must use Django Admin, not client APIs.
        if user.role == User.Role.SUPER_ADMIN:
            raise AuthenticationFailed(
                "Super Admin cannot access client APIs."
            )

        # Every client API request requires an active business context.
        active_business_id = token.get("active_business_id")

        if not active_business_id:
            raise AuthenticationFailed(
                "Active business context is missing. Please log in again."
            )

        business = (
            Business.objects
            .select_related("company")
            .filter(pk=active_business_id)
            .first()
        )

        # Tenant, role, Position and active-state rules live in one place.
        # The access mode is derived from database state, never from the
        # client or from claims inside the token.
        try:
            access_mode = validate_user_access(user, business)
        except AccessDenied as exc:
            raise AuthenticationFailed(exc.message)

        # A user who must set a new password (for example after an admin
        # reset) can do nothing else until they have changed it.
        if (
            user.must_change_password
            and not self._is_password_change_view(request)
        ):
            raise PasswordChangeRequired()

        # Make the validated context available to API views and permissions.
        request.active_business = business
        request.access_mode = access_mode

        # Tell the audit log who is acting, and in which business.
        audit_context.identify(user, business)

        return user, token

    @staticmethod
    def _is_password_change_view(request):
        # If the view cannot be determined the request is not allowed
        # (fail closed).
        match = getattr(request, "resolver_match", None)

        return (
            match is not None
            and match.view_name in PASSWORD_CHANGE_ALLOWED_VIEWS
        )
