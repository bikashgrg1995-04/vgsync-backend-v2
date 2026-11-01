
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
    LoginAccountRateThrottle,
    LoginIPRateThrottle,
    TokenIPRateThrottle,
)

from .access import serialize_active_business
from .serializers import (
    ChangePasswordSerializer,
    LogoutSerializer,
    UserProfileSerializer,
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

        # VGSyncJWTAuthentication has already validated the active business
        # and the user's access to it against the current database state
        # (using apps.accounts.access.validate_user_access).
        return Response({
            "user": self.get_serializer(user).data,
            "active_business": serialize_active_business(
                request.active_business,
                request.access_mode,
            ),
        })


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
    LoginAccountRateThrottle,
    LoginIPRateThrottle,
    TokenIPRateThrottle,
)

from .access import serialize_active_business
from .serializers import (
    BusinessContextSwitchSerializer,
    ChangePasswordSerializer,
    LogoutSerializer,
    UserProfileSerializer,
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

        # VGSyncJWTAuthentication has already validated the active business
        # and the user's access to it against the current database state.
        return Response({
            "user": self.get_serializer(user).data,
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
