
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

from apps.businesses.models import Business

from .serializers import (
    ChangePasswordSerializer,
    UserProfileSerializer,
    VGSyncTokenObtainPairSerializer,
    VGSyncTokenRefreshSerializer,
)


class LoginView(TokenObtainPairView):
    permission_classes = [AllowAny]
    serializer_class = VGSyncTokenObtainPairSerializer


class RefreshTokenView(TokenRefreshView):
    permission_classes = [AllowAny]
    serializer_class = VGSyncTokenRefreshSerializer


class CurrentUserView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UserProfileSerializer

    def get_object(self):
        return (
            self.request.user.__class__.objects
            .select_related(
                "company",
                "business",
                "position",
                "position__business",
            )
            .get(pk=self.request.user.pk)
        )

    def retrieve(self, request, *args, **kwargs):
        user = self.get_object()

        # Active business context must come from the JWT.
        active_business_id = request.auth.get("active_business_id")

        if not active_business_id:
            return Response(
                {
                    "detail": (
                        "Active business context is missing. "
                        "Please log in again."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
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
            return Response(
                {
                    "detail": (
                        "The selected business is unavailable. "
                        "Please log in again."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # Company must remain active.
        if business.company_id and not business.company.is_active:
            return Response(
                {"detail": "The selected company is inactive."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Validate that the user still has access to this business.
        if user.company_id:

            if (
                business.company_id != user.company_id
                or user.tenant_company_id != user.company_id
            ):
                return Response(
                    {"detail": "You are not allowed to access this business."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            if user.role == user.Role.STAFF:
                if (
                    business.business_type
                    != Business.BusinessType.HEAD_OFFICE
                ):
                    return Response(
                        {
                            "detail": (
                                "Company Staff can only access Head Office."
                            )
                        },
                        status=status.HTTP_403_FORBIDDEN,
                    )

            elif user.role not in [
                user.Role.OWNER,
                user.Role.ADMIN,
                user.Role.ACCOUNTANT,
            ]:
                return Response(
                    {"detail": "Your current role is not authorized."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        elif user.business_id:

            if (
                business.id != user.business_id
                or user.tenant_company_id != business.company_id
            ):
                return Response(
                    {"detail": "You are not allowed to access this business."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            if user.role not in [
                user.Role.OWNER,
                user.Role.ADMIN,
                user.Role.ACCOUNTANT,
                user.Role.STAFF,
            ]:
                return Response(
                    {"detail": "Your current role is not authorized."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        else:
            return Response(
                {"detail": "This account has no valid business assignment."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Staff must retain a valid active Position.
        if user.role == user.Role.STAFF:

            if (
                not user.position_id
                or not user.position.is_active
            ):
                return Response(
                    {
                        "detail": (
                            "Your account does not have a valid active position."
                        )
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

            expected_position_business_id = (
                user.business_id
                if user.business_id
                else business.id
            )

            if (
                user.position.business_id
                != expected_position_business_id
            ):
                return Response(
                    {
                        "detail": (
                            "Your account does not have a valid active position."
                        )
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

        # Determine access mode from current database state.
        access_mode = (
            "read_only"
            if (
                user.company_id
                and business.business_type == Business.BusinessType.BRANCH
            )
            else "full"
        )

        return Response({
            "user": self.get_serializer(user).data,
            "active_business": {
                "id": business.id,
                "name": business.name,
                "business_code": business.business_code,
                "business_type": business.business_type,
                "company_id": business.company_id,
                "company_name": (
                    business.company.name if business.company else None
                ),
                "access_mode": access_mode,
            },
        })


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(
            data=request.data,
            context={"request": request},
        )

        serializer.is_valid(raise_exception=True)
        serializer.save(user=request.user)

        return Response(
            {"detail": "Password changed successfully."},
            status=status.HTTP_200_OK,
        )
