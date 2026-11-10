"""
Serializers for the owner's staff and Position management APIs.
"""

import secrets

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from rest_framework import serializers

from apps.businesses.models import Business

from . import rbac
from .models import (
    Position,
    PositionPermission,
    StaffPermissionOverride,
    User,
)
from .scope import MANAGEABLE_ROLES, manageable_position_ids, username_taken


def generate_temporary_password():
    """A random one-time password the owner hands to the new person."""
    return secrets.token_urlsafe(9)


def validate_new_password(password, user=None):
    # The similarity validator needs a user to compare against.
    validate_password(password, user=user or User())


# ---------------------------------------------------------------------------
# Staff
# ---------------------------------------------------------------------------

class StaffSerializer(serializers.ModelSerializer):
    """What the owner sees about a team member."""

    position_name = serializers.CharField(
        source="position.name",
        read_only=True,
        default=None,
    )

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "first_name",
            "last_name",
            "email",
            "phone",
            "role",
            "position",
            "position_name",
            "is_active",
            "must_change_password",
            "last_login",
            "date_joined",
        )
        read_only_fields = fields


ROLE_CHOICES = [(role.value, role.label) for role in MANAGEABLE_ROLES]


class _StaffInputSerializer(serializers.ModelSerializer):
    """Fields and rules shared by creating and editing a team member."""

    # Declared explicitly (not taken from the model field) so only the roles
    # an owner may hand out are accepted, and so "required" can differ
    # between create and edit without clashing with the model's default.
    role = serializers.ChoiceField(choices=ROLE_CHOICES, required=False)

    position = serializers.PrimaryKeyRelatedField(
        queryset=Position.objects.none(),
        required=False,
        allow_null=True,
        error_messages={
            "does_not_exist": "Choose an active position of this business.",
            "incorrect_type": "Choose an active position of this business.",
        },
    )

    class Meta:
        model = User
        fields = (
            "first_name",
            "last_name",
            "email",
            "phone",
            "role",
            "position",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        request = self.context["request"]
        business = request.active_business

        positions = Position.objects.filter(business=business, is_active=True)

        if request.user.role != User.Role.OWNER:
            # A delegated administrator cannot hand out more than they have.
            positions = positions.filter(
                pk__in=manageable_position_ids(request.user, business)
            )

        self.fields["position"].queryset = positions

    @property
    def business(self):
        return self.context["request"].active_business

    def validate(self, attrs):
        instance = self.instance

        role = attrs.get("role", instance.role if instance else None)

        if (
            self.context["request"].user.role != User.Role.OWNER
            and role != User.Role.STAFF
        ):
            raise serializers.ValidationError(
                {"role": "You can only manage staff accounts."}
            )

        if "position" in attrs:
            position = attrs["position"]

            # Picking a Position while creating someone is part of creating
            # them; moving an existing person needs ``assign_position``.
            changing = instance is not None and position != instance.position
            requester = self.context["request"].user

            # Choosing a Position is its own permission for delegated staff.
            if (
                changing
                and requester.role != User.Role.OWNER
                and not rbac.has_permission(
                    requester,
                    "staff.assign_position",
                    getattr(self.context["request"], "access_mode", "full"),
                )
            ):
                raise serializers.ValidationError(
                    {"position": "You may not assign positions."}
                )
        else:
            position = instance.position if instance else None

        if role == User.Role.STAFF:
            if position is None:
                raise serializers.ValidationError(
                    {"position": "Staff must be assigned a position."}
                )

        else:
            if attrs.get("position") is not None:
                raise serializers.ValidationError(
                    {"position": "Only staff can be assigned a position."}
                )

            # Moving someone out of the staff role drops their position.
            attrs["position"] = None

        return attrs


class StaffCreateSerializer(_StaffInputSerializer):
    role = serializers.ChoiceField(
        choices=ROLE_CHOICES, required=False, default=User.Role.STAFF
    )

    password = serializers.CharField(
        write_only=True,
        required=False,
        trim_whitespace=False,
        help_text=(
            "Optional. When left out, a random temporary password is "
            "generated and returned once."
        ),
    )

    class Meta(_StaffInputSerializer.Meta):
        fields = ("username",) + _StaffInputSerializer.Meta.fields + (
            "password",
        )

    def validate_username(self, value):
        value = value.strip()

        if username_taken(self.business, value):
            raise serializers.ValidationError(
                "This username is already used in your organization."
            )

        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)

        password = attrs.get("password")

        if password is not None:
            try:
                validate_new_password(
                    password,
                    user=User(
                        username=attrs["username"],
                        first_name=attrs.get("first_name", ""),
                        last_name=attrs.get("last_name", ""),
                        email=attrs.get("email", ""),
                    ),
                )
            except DjangoValidationError as exc:
                # Keyed by field so a form can show it beside the password.
                raise serializers.ValidationError(
                    {"password": list(exc.messages)}
                )

        return attrs

    def create(self, validated_data):
        business = self.business

        password = validated_data.pop("password", None)
        generated = password is None

        if generated:
            password = generate_temporary_password()

        user = User(**validated_data)

        # Company users are created from the Head Office; everyone else
        # belongs directly to the business they are created in.
        if business.business_type == Business.BusinessType.HEAD_OFFICE:
            user.company_id = business.company_id
        else:
            user.business = business

        user.set_password(password)
        user.must_change_password = True

        try:
            with transaction.atomic():
                user.save()
        except IntegrityError:
            # Lost a race with another request creating the same username.
            raise serializers.ValidationError(
                {"username": "This username is already used in your organization."}
            )

        self.temporary_password = password if generated else None

        return user


class StaffUpdateSerializer(_StaffInputSerializer):
    """Edit details or assign a position. Username is fixed."""

    @transaction.atomic
    def update(self, instance, validated_data):
        old_position_id = instance.position_id

        user = super().update(instance, validated_data)

        # A new Position starts clean: one-person exceptions made for the
        # old Position are not carried over silently.
        if user.position_id != old_position_id:
            user.permission_overrides.all().delete()

        return user


class OverrideItemSerializer(serializers.Serializer):
    permission = serializers.CharField()
    effect = serializers.ChoiceField(
        choices=["allow", "deny", "inherit"]
    )

    def validate_permission(self, value):
        if not rbac.is_valid_code(value):
            raise serializers.ValidationError("Unknown permission.")

        if value in rbac.RESERVED:
            raise serializers.ValidationError(
                "This permission belongs to the owner only."
            )

        return value


class StaffOverridesSerializer(serializers.Serializer):
    """``overrides``: [{"permission": "sales.cancel", "effect": "allow"}]."""

    overrides = OverrideItemSerializer(many=True)

    def validate_overrides(self, value):
        codes = [item["permission"] for item in value]

        if len(codes) != len(set(codes)):
            raise serializers.ValidationError(
                "Each permission can appear only once."
            )

        return value


class ResetPasswordSerializer(serializers.Serializer):
    password = serializers.CharField(
        required=False,
        trim_whitespace=False,
        help_text=(
            "Optional. When left out, a random temporary password is "
            "generated and returned once."
        ),
    )

    def validate_password(self, value):
        validate_new_password(value, user=self.context.get("target"))

        return value


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

class PositionPermissionSerializer(serializers.ModelSerializer):
    """Read shape of one module's grants (flags plus finer actions)."""

    class Meta:
        model = PositionPermission
        fields = (
            "module",
            "can_view",
            "can_create",
            "can_update",
            "can_delete",
            "actions",
        )

    actions = serializers.ListField(
        child=serializers.CharField(), required=False
    )

    def validate(self, attrs):
        module = attrs["module"]
        wants_write = any(
            attrs.get(flag, False)
            for flag in ("can_create", "can_update", "can_delete")
        ) or bool(attrs.get("actions"))

        if wants_write and not attrs.get("can_view", False):
            raise serializers.ValidationError(
                "View permission is required when create, update or "
                "delete is allowed."
            )

        for action in attrs.get("actions", []):
            code = f"{module}.{action}"

            if not rbac.is_valid_code(code):
                raise serializers.ValidationError(f"Unknown permission: {code}.")

            if code in rbac.RESERVED:
                raise serializers.ValidationError(
                    f"{code} belongs to the owner only."
                )

        return attrs


class PositionSerializer(serializers.ModelSerializer):
    permissions = PositionPermissionSerializer(many=True, required=False)
    # Write: the full list of ``module.action`` codes this Position grants.
    codes = serializers.ListField(
        child=serializers.CharField(), required=False, write_only=True
    )
    # Write once, on create: start from a preset's grants.
    preset = serializers.ChoiceField(
        choices=[(key, key) for key in rbac.PRESETS],
        required=False,
        allow_blank=True,
    )
    granted = serializers.SerializerMethodField()
    staff_count = serializers.SerializerMethodField()

    class Meta:
        model = Position
        fields = (
            "id",
            "name",
            "description",
            "department",
            "preset",
            "is_active",
            "permissions",
            "codes",
            "granted",
            "staff_count",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "staff_count", "created_at", "updated_at")

    @property
    def business(self):
        return self.context["request"].active_business

    def get_granted(self, obj):
        return sorted(rbac.position_codes(obj))

    def get_staff_count(self, obj):
        count = getattr(obj, "staff_count", None)

        if count is None:
            count = obj.users.filter(is_active=True).count()

        return count

    def validate_name(self, value):
        value = value.strip()

        taken = Position.objects.filter(
            business=self.business,
            name__iexact=value,
        )

        if self.instance:
            taken = taken.exclude(pk=self.instance.pk)

        if taken.exists():
            raise serializers.ValidationError(
                "A position with this name already exists."
            )

        return value

    def validate_codes(self, value):
        for code in value:
            if not rbac.is_valid_code(code):
                raise serializers.ValidationError(f"Unknown permission: {code}.")

            if code in rbac.RESERVED:
                raise serializers.ValidationError(
                    f"{code} belongs to the owner only."
                )

        return sorted(set(value))

    def validate(self, attrs):
        if (
            self.instance
            and attrs.get("is_active") is False
            and self.instance.is_active
        ):
            active_staff = self.instance.users.filter(is_active=True).count()

            if active_staff:
                raise serializers.ValidationError({
                    "is_active": (
                        f"{active_staff} active staff still use this "
                        "position. Assign them another position first."
                    )
                })

        return attrs

    @staticmethod
    def _codes(validated_data, instance=None):
        """Codes to store: explicit ``codes`` win over a preset."""
        codes = validated_data.pop("codes", None)
        preset = validated_data.get("preset")

        if codes is None and preset and instance is None:
            codes = sorted(rbac.preset_codes(preset) - rbac.RESERVED)

        return codes

    @staticmethod
    def _save_permissions(position, permissions):
        """Per-module flags: listed modules are set exactly, others kept."""
        for item in permissions:
            actions = sorted(set(item.get("actions", [])))

            PositionPermission.objects.update_or_create(
                position=position,
                module=item["module"],
                defaults={
                    "can_view": item.get("can_view", False),
                    "can_create": item.get("can_create", False),
                    "can_update": item.get("can_update", False),
                    "can_delete": item.get("can_delete", False),
                    "actions": actions,
                },
            )

    def validate_permissions(self, value):
        modules = [item["module"] for item in value]

        if len(modules) != len(set(modules)):
            raise serializers.ValidationError(
                "Each module can appear only once."
            )

        return value

    @transaction.atomic
    def create(self, validated_data):
        codes = self._codes(validated_data)
        permissions = validated_data.pop("permissions", [])

        preset = validated_data.get("preset")

        if preset and not validated_data.get("department"):
            validated_data["department"] = rbac.PRESETS[preset]["department"]

        position = Position.objects.create(
            business=self.business,
            **validated_data,
        )

        if codes:
            rbac.write_position_codes(position, codes)

        self._save_permissions(position, permissions)

        return position

    @transaction.atomic
    def update(self, instance, validated_data):
        codes = self._codes(validated_data, instance)
        permissions = validated_data.pop("permissions", None)
        validated_data.pop("preset", None)

        for field, value in validated_data.items():
            setattr(instance, field, value)

        instance.save()

        # Everyone holding the Position inherits the change at once; their
        # own overrides stay as they are.
        if codes is not None:
            rbac.write_position_codes(instance, codes)

        if permissions is not None:
            self._save_permissions(instance, permissions)

        return instance
