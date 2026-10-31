
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

from .access import serialize_active_business
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
