
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.businesses.models import Business
from apps.accounts.models import User


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
            .filter(
                pk=active_business_id,
                is_active=True,
            )
            .first()
        )

        if not business:
            raise AuthenticationFailed(
                "The selected business is unavailable."
            )

        # The parent company must remain active.
        if business.company_id and not business.company.is_active:
            raise AuthenticationFailed(
                "The selected company is inactive."
            )

        # Validate the user's current tenant assignment.
        if user.company_id:

            if user.company_id != business.company_id:
                raise AuthenticationFailed(
                    "You are not authorized to access this business."
                )

            if user.tenant_company_id != user.company_id:
                raise AuthenticationFailed(
                    "Your company assignment is invalid."
                )

            if user.role == User.Role.STAFF:
                if business.business_type != Business.BusinessType.HEAD_OFFICE:
                    raise AuthenticationFailed(
                        "Company Staff can only access Head Office."
                    )

            elif user.role not in [
                User.Role.OWNER,
                User.Role.ADMIN,
                User.Role.ACCOUNTANT,
            ]:
                raise AuthenticationFailed(
                    "Your role is not authorized for this access."
                )

        elif user.business_id:

            if user.business_id != business.id:
                raise AuthenticationFailed(
                    "You are not authorized to access this business."
                )

            if user.tenant_company_id != business.company_id:
                raise AuthenticationFailed(
                    "Your business assignment is invalid."
                )

        else:
            raise AuthenticationFailed(
                "Your account has no valid business assignment."
            )

        # Staff must have an active Position belonging to the correct business.
        if user.role == User.Role.STAFF:

            if not user.position_id:
                raise AuthenticationFailed(
                    "Your account does not have an assigned position."
                )

            position = user.position

            if not position.is_active:
                raise AuthenticationFailed(
                    "Your assigned position is inactive."
                )

            expected_business_id = (
                user.business_id
                if user.business_id
                else business.id
            )

            if position.business_id != expected_business_id:
                raise AuthenticationFailed(
                    "Your assigned position is invalid."
                )

        # Calculate access mode from database state, not from client input.
        access_mode = "full"

        if (
            user.company_id
            and business.business_type == Business.BusinessType.BRANCH
        ):
            access_mode = "read_only"

        # Make the validated context available to API views and permissions.
        request.active_business = business
        request.access_mode = access_mode

        return user, token
