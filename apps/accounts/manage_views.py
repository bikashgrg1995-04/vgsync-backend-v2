"""
Owner-only APIs to manage the team and its Positions.

Everything is scoped to the active business (see ``scope.py``) and is
available only to the owner (``IsOwner``). A user who still has to change
their password is blocked from these endpoints by the authentication class
like every other API.
"""

from django.db import transaction
from django.db.models import Count, Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.common.exceptions import Conflict
from apps.common.permissions import IsOwner

from .manage_serializers import (
    PositionSerializer,
    ResetPasswordSerializer,
    StaffCreateSerializer,
    StaffSerializer,
    StaffUpdateSerializer,
    generate_temporary_password,
)
from .models import PositionPermission, User
from .scope import scoped_positions, scoped_users
from .tokens import revoke_all_refresh_tokens


class StaffViewSet(viewsets.ModelViewSet):
    """
    Team members of the active business.

    ``DELETE`` deactivates the account instead of removing it: the person
    can no longer sign in, but their history stays intact. Use
    ``reactivate`` to bring them back.
    """

    permission_classes = [IsOwner]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    filterset_fields = ("role", "is_active", "position")
    search_fields = ("username", "first_name", "last_name", "email", "phone")
    ordering_fields = ("username", "first_name", "date_joined", "last_login")
    ordering = ("username",)

    def get_queryset(self):
        return scoped_users(self.request.active_business).select_related(
            "position"
        )

    def get_serializer_class(self):
        if self.action == "create":
            return StaffCreateSerializer

        if self.action in ("update", "partial_update"):
            return StaffUpdateSerializer

        return StaffSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = serializer.save()

        data = StaffSerializer(user).data

        # Shown once: only a hash is stored, so it cannot be looked up later.
        if serializer.temporary_password:
            data["temporary_password"] = serializer.temporary_password

        return Response(data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        user = self.get_object()

        serializer = self.get_serializer(
            user,
            data=request.data,
            partial=partial,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        # A role or position change can change what the person may do, but
        # they stay signed in; the new rules apply on their next request
        # because access is re-checked against the database every time.
        user.refresh_from_db()

        return Response(StaffSerializer(user).data)

    def destroy(self, request, *args, **kwargs):
        user = self.get_object()

        with transaction.atomic():
            if user.is_active:
                user.is_active = False
                user.save(update_fields=["is_active"])

            # Their access tokens stop working at once because inactive
            # users are rejected; revoking refresh tokens stops new ones.
            revoke_all_refresh_tokens(user)

        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def reactivate(self, request, pk=None):
        user = self.get_object()

        if user.role == User.Role.STAFF and (
            not user.position_id or not user.position.is_active
        ):
            raise Conflict(
                "Assign an active position before reactivating this staff.",
                code="position_required",
            )

        if not user.is_active:
            user.is_active = True
            user.save(update_fields=["is_active"])

        return Response(StaffSerializer(user).data)

    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request, pk=None):
        user = self.get_object()

        serializer = ResetPasswordSerializer(
            data=request.data,
            context={"target": user},
        )
        serializer.is_valid(raise_exception=True)

        password = serializer.validated_data.get("password")
        generated = password is None

        if generated:
            password = generate_temporary_password()

        with transaction.atomic():
            user.set_password(password)
            user.must_change_password = True
            user.save(update_fields=["password", "must_change_password"])

            # Whoever knew the old password is signed out everywhere.
            revoke_all_refresh_tokens(user)

        data = {"detail": "Password reset. The user must change it at next login."}

        if generated:
            data["temporary_password"] = password

        return Response(data)


class PositionViewSet(viewsets.ModelViewSet):
    """
    Positions of the active business and the module permissions each one
    grants. Staff are assigned a Position through the staff API.
    """

    permission_classes = [IsOwner]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    serializer_class = PositionSerializer

    filterset_fields = ("is_active",)
    search_fields = ("name", "description")
    ordering_fields = ("name", "created_at")
    ordering = ("name",)

    def get_queryset(self):
        return (
            scoped_positions(self.request.active_business)
            .prefetch_related("permissions")
            .annotate(
                staff_count=Count("users", filter=Q(users__is_active=True))
            )
        )

    def destroy(self, request, *args, **kwargs):
        position = self.get_object()

        assigned = position.users.count()

        if assigned:
            raise Conflict(
                f"{assigned} staff are assigned to this position. "
                "Assign them another position first.",
                code="position_in_use",
            )

        position.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["get"])
    def modules(self, request):
        """The modules a Position can grant permissions on."""
        return Response([
            {"module": value, "label": label}
            for value, label in PositionPermission.Module.choices
        ])
