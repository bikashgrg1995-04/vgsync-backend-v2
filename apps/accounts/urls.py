
from django.urls import path

from .views import (
    BusinessContextSwitchView,
    ChangePasswordView,
    CurrentUserView,
    LoginView,
    LogoutView,
    RefreshTokenView,
)


app_name = "accounts"

urlpatterns = [
    path("login/", LoginView.as_view(), name="login"),
    path("token/refresh/", RefreshTokenView.as_view(), name="token-refresh"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("me/", CurrentUserView.as_view(), name="current-user"),
    path(
        "business-context/",
        BusinessContextSwitchView.as_view(),
        name="business-context-switch",
    ),
    path("change-password/", ChangePasswordView.as_view(), name="change-password"),
]
