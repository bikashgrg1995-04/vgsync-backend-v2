
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.accounts.access import AccessDenied, validate_user_access
from apps.accounts.models import User
from apps.businesses.models import Business


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

        # Make the validated context available to API views and permissions.
        request.active_business = business
        request.access_mode = access_mode

        return user, token
