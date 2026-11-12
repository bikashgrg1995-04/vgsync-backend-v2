"""
APIs to manage the team and its Positions.

Everything is scoped to the active business (see ``scope.py``). The owner
can do everything. Staff whose Position grants the "staff" module (the
"Administration" position) can manage other staff accounts, but only staff
accounts whose position grants no more than their own, never themselves,
and they cannot create positions (they may read the ones they can assign). A user who still has to change
their password is blocked from these endpoints by the authentication class
like every other API.
"""

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed
from rest_framework.response import Response

from apps.common.exceptions import Conflict
from apps.common.permissions import CanManageTeam, IsOwner

from . import rbac
from .manage_serializers import (
    PositionSerializer,
    ResetPasswordSerializer,
    StaffCreateSerializer,
    StaffOverridesSerializer,
    StaffSerializer,
    StaffUpdateSerializer,
    generate_temporary_password,
)
from .models import PositionPermission, StaffPermissionOverride, User
from .scope import manageable_position_ids, scoped_positions, scoped_users
from .tokens import revoke_all_refresh_tokens


class StaffViewSet(viewsets.ModelViewSet):
    """
    Team members of the active business.

    ``DELETE`` deactivates the account instead of removing it: the person
    can no longer sign in, but their history stays intact. Use
    ``reactivate`` to bring them back.
    """

    permission_classes = [CanManageTeam]
    http_method_names = [
        "get", "post", "put", "patch", "delete", "head", "options",
    ]
    # Which ``staff.*`` action each custom endpoint needs.
    permission_actions = {
        "reactivate": "update",
        "reset_password": "update",
        "access": "view",
    }

    def get_permissions(self):
        # Changing someone's permissions is reserved for the owner.
        if self.action == "access" and self.request.method == "PUT":
            return [IsOwner()]

        return super().get_permissions()

    filterset_fields = ("role", "is_active", "position")
    search_fields = ("username", "first_name", "last_name", "email", "phone")
    ordering_fields = ("username", "first_name", "date_joined", "last_login")
    ordering = ("username",)

    def get_queryset(self):
        business = self.request.active_business
        user = self.request.user

        queryset = scoped_users(business).select_related("position")

        if user.role == User.Role.OWNER:
            return queryset

        # A delegated administrator manages staff accounts only: not
        # themselves, and not anyone with more access than they have.
        allowed = manageable_position_ids(user, business)

        return (
            queryset
            .filter(role=User.Role.STAFF)
            .exclude(pk=user.pk)
            .filter(Q(position_id__in=allowed) | Q(position__isnull=True))
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

        # PUT is only used by the ``access`` endpoint; editing a person is PATCH.
        if not partial:
            raise MethodNotAllowed(request.method)

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
                # Record salary due up to today before the person stops
                # earning it.
                profile = getattr(user, "salary_profile", None)

                if profile is not None:
                    from apps.payroll.services import ensure_accruals

                    ensure_accruals(profile)

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

            # Time spent inactive earns no monthly salary.
            profile = getattr(user, "salary_profile", None)

            if profile is not None:
                profile.accrued_through = timezone.localdate()
                profile.save(update_fields=["accrued_through"])

        return Response(StaffSerializer(user).data)

    @action(detail=True, methods=["get", "put"])
    def access(self, request, pk=None):
        """
        Access & Permissions of one person: what their Position gives,
        their own overrides and the result. ``PUT`` (owner only) replaces
        the overrides: ``{"overrides": [{"permission": "sales.cancel",
        "effect": "allow" | "deny" | "inherit"}]}``.
        """
        user = self.get_object()

        if request.method == "PUT":
            serializer = StaffOverridesSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            with transaction.atomic():
                for item in serializer.validated_data["overrides"]:
                    if item["effect"] == "inherit":
                        user.permission_overrides.filter(
                            permission=item["permission"]
                        ).delete()
                    else:
                        StaffPermissionOverride.objects.update_or_create(
                            user=user,
                            permission=item["permission"],
                            defaults={
                                "effect": item["effect"],
                                "created_by": request.user,
                            },
                        )

            user = self.get_object()

        data = rbac.breakdown(user)
        data["position"] = user.position_id

        return Response(data)

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

    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    serializer_class = PositionSerializer

    def get_permissions(self):
        # Reading is shared with delegated administrators (they need the list
        # to assign positions); changing positions stays with the owner.
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [CanManageTeam()]

        return [IsOwner()]

    filterset_fields = ("is_active",)
    search_fields = ("name", "description")
    ordering_fields = ("name", "created_at")
    ordering = ("name",)

    def get_queryset(self):
        business = self.request.active_business
        user = self.request.user

        queryset = scoped_positions(business)

        if user.role != User.Role.OWNER:
            # Only positions they could assign anyway.
            queryset = queryset.filter(
                pk__in=manageable_position_ids(user, business)
            )

        return queryset.prefetch_related("permissions").annotate(
            staff_count=Count("users", filter=Q(users__is_active=True))
        )

    def destroy(self, request, *args, **kwargs):
        position = self.get_object()

        # Only people who can still log in hold a position in use. Those who
        # were switched off just lose it; their records are untouched.
        assigned = position.users.filter(is_active=True).count()

        if assigned:
            raise Conflict(
                f"{assigned} staff are assigned to this position. "
                "Assign them another position first.",
                code="position_in_use",
            )

        with transaction.atomic():
            position.users.filter(is_active=False).update(position=None)
            position.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["get"])
    def presets(self, request):
        """Starting templates for a new Position (never kept in sync)."""
        return Response(rbac.presets())

    @action(detail=False, methods=["get"])
    def catalogue(self, request):
        """Every ``module.action`` permission, grouped by module."""
        return Response(rbac.catalogue())

    @action(detail=False, methods=["get"])
    def modules(self, request):
        """The modules a Position can grant permissions on."""
        return Response([
            {"module": value, "label": label}
            for value, label in PositionPermission.Module.choices
        ])
