from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

from apps.common.throttling import (
    ChangePasswordRateThrottle,
    ForgotPasswordAccountRateThrottle,
    ForgotPasswordIPRateThrottle,
    ResetPasswordRateThrottle,
    LoginAccountRateThrottle,
    LoginIPRateThrottle,
    TokenIPRateThrottle,
)

from .access import serialize_active_business
from .serializers import (
    BusinessContextSwitchSerializer,
    ChangePasswordSerializer,
    ForgotPasswordSerializer,
    LogoutSerializer,
    ResetPasswordSerializer,
    UserProfileSerializer,
    UserProfileUpdateSerializer,
    VGSyncTokenObtainPairSerializer,
    VGSyncTokenRefreshSerializer,
)


class LoginView(TokenObtainPairView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginIPRateThrottle, LoginAccountRateThrottle]
    serializer_class = VGSyncTokenObtainPairSerializer


class RefreshTokenView(TokenRefreshView):
    permission_classes = [AllowAny]
    throttle_classes = [TokenIPRateThrottle]
    serializer_class = VGSyncTokenRefreshSerializer


class LogoutView(APIView):
    """
    Revokes the given refresh token. It takes only the refresh token (no
    access token), so a user whose access token has expired can still
    sign out. Logging out twice is not an error.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [TokenIPRateThrottle]

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)

        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(
            {"detail": "Logged out."},
            status=status.HTTP_200_OK,
        )


class CurrentUserView(generics.RetrieveUpdateAPIView):
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

    def get_serializer_class(self):
        if self.request.method in ("PATCH", "PUT"):
            return UserProfileUpdateSerializer

        return UserProfileSerializer

    def update(self, request, *args, **kwargs):
        """Change your own name, email and phone. Nothing else."""
        user = self.get_object()
        serializer = UserProfileUpdateSerializer(
            user, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return self.retrieve(request)

    def retrieve(self, request, *args, **kwargs):
        user = self.get_object()

        # VGSyncJWTAuthentication has already validated the active business
        # and the user's access to it against the current database state.
        return Response({
            "user": UserProfileSerializer(user).data,
            "active_business": serialize_active_business(
                request.active_business,
                request.access_mode,
            ),
        })


class BusinessContextSwitchView(APIView):
    """
    Switch the authenticated user's active business context.

    The serializer validates the supplied refresh token, verifies access
    to the target business, blacklists the old refresh token and issues
    new tokens containing the selected business context.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = BusinessContextSwitchSerializer(
            data=request.data,
            context={"request": request},
        )

        serializer.is_valid(raise_exception=True)

        return Response(
            serializer.validated_data,
            status=status.HTTP_200_OK,
        )


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ChangePasswordRateThrottle]

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


class ForgotPasswordView(APIView):
    """
    Emails a one-time reset link to the account's email address. The reply
    is always the same, whether or not the account exists or has an email.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [
        ForgotPasswordIPRateThrottle,
        ForgotPasswordAccountRateThrottle,
    ]

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)

        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(
            {
                "detail": (
                    "If the account has an email address, a password reset "
                    "link has been sent. Otherwise ask your owner or admin "
                    "to reset your password."
                )
            },
            status=status.HTTP_200_OK,
        )


class ResetPasswordView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ResetPasswordRateThrottle]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)

        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(
            {"detail": "Password reset. Please log in with your new password."},
            status=status.HTTP_200_OK,
        )
