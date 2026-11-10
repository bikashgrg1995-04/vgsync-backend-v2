
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.db import transaction
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from rest_framework import serializers

from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.serializers import (
    TokenObtainPairSerializer,
    TokenRefreshSerializer,
)
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from apps.businesses.models import Business

from .access import (
    AccessDenied,
    serialize_active_business,
    validate_user_access,
)
from . import rbac
from .models import User
from .tokens import revoke_all_refresh_tokens


class UserProfileSerializer(serializers.ModelSerializer):
    scope = serializers.CharField(read_only=True)
    company_name = serializers.SerializerMethodField()
    business_name = serializers.SerializerMethodField()
    position_name = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()
    can_manage_team = serializers.SerializerMethodField()
    effective_permissions = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "phone",
            "role",
            "scope",
            "company_name",
            "business_name",
            "position_name",
            "permissions",
            "can_manage_team",
            "effective_permissions",
            "must_change_password",
        )
        read_only_fields = fields

    def get_company_name(self, obj):
        return obj.company.name if obj.company else None

    def get_business_name(self, obj):
        return obj.business.name if obj.business else None

    def get_position_name(self, obj):
        return obj.position.name if obj.position else None

    def _codes(self, obj):
        return rbac.effective_permissions(obj)

    def get_permissions(self, obj):
        """
        What a staff member may do per module (Position grants with the
        person's own overrides applied), so screens can hide the rest.
        """
        if obj.role != User.Role.STAFF:
            return []

        codes = self._codes(obj)
        modules = sorted({code.split(".", 1)[0] for code in codes})

        return [
            {
                "module": module,
                "can_view": f"{module}.view" in codes,
                "can_create": f"{module}.create" in codes,
                "can_update": f"{module}.update" in codes,
                "can_delete": f"{module}.delete" in codes,
                "actions": sorted(
                    code.split(".", 1)[1]
                    for code in codes
                    if code.startswith(f"{module}.")
                    and code.split(".", 1)[1] not in rbac.FLAG_ACTIONS
                ),
            }
            for module in modules
        ]

    def get_effective_permissions(self, obj):
        """Every ``module.action`` code the person holds right now."""
        return sorted(self._codes(obj))

    def get_can_manage_team(self, obj):
        """Owner, or staff who may view the team."""
        if obj.role == User.Role.OWNER:
            return True

        return "staff.view" in self._codes(obj)


class UserProfileUpdateSerializer(serializers.ModelSerializer):
    """The few fields a person may change about themselves."""

    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "phone")
        extra_kwargs = {
            "first_name": {"required": False, "max_length": 150},
            "last_name": {"required": False, "max_length": 150},
        }

    def validate_phone(self, value):
        value = value.strip()

        if value and not all(c.isdigit() or c in "+- ()" for c in value):
            raise serializers.ValidationError("Enter a valid phone number.")

        return value


class VGSyncTokenObtainPairSerializer(TokenObtainPairSerializer):
    business_code = serializers.CharField(max_length=50)
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(
        write_only=True,
        trim_whitespace=False,
    )

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)

        token["role"] = user.role
        token["scope"] = user.scope

        if user.company_id:
            token["company_id"] = user.company_id

        if user.business_id:
            token["business_id"] = user.business_id

        if user.position_id:
            token["position_id"] = user.position_id

        return token

    def validate(self, attrs):
        business_code = attrs["business_code"].strip()
        username = attrs["username"].strip()
        password = attrs["password"]

        # 1. Find the selected business.
        business = (
            Business.objects
            .select_related("company")
            .filter(
                business_code__iexact=business_code,
                is_active=True,
            )
            .first()
        )

        if not business:
            raise serializers.ValidationError(
                {"detail": "Invalid business code or login credentials."}
            )

        # 2. Company must also be active.
        if business.company_id and not business.company.is_active:
            raise serializers.ValidationError(
                {"detail": "Invalid business code or login credentials."}
            )

        # 3. Find the user within the correct username namespace.
        users = (
            User.objects
            .select_related(
                "company",
                "business",
                "position",
                "position__business",
            )
            .filter(
                username=username,
                is_active=True,
            )
            .exclude(
                role=User.Role.SUPER_ADMIN,
            )
        )

        if business.company_id:
            user = users.filter(
                tenant_company_id=business.company_id,
            ).first()
        else:
            user = users.filter(
                business_id=business.id,
                tenant_company__isnull=True,
            ).first()

        if not user or not user.check_password(password):
            raise serializers.ValidationError(
                {"detail": "Invalid business code or login credentials."}
            )

        # 4. Validate the user's scope, role and Position for this business.
        try:
            access_mode = validate_user_access(user, business)
        except AccessDenied as exc:
            raise serializers.ValidationError({"detail": exc.message})

        # 5. Generate JWT with selected business context.
        refresh = self.get_token(user)

        refresh["active_business_id"] = business.id
        refresh["active_business_code"] = business.business_code
        refresh["active_business_type"] = business.business_type
        refresh["active_company_id"] = business.company_id
        refresh["access_mode"] = access_mode

        access = refresh.access_token

        self.user = user

        return {
            "refresh": str(refresh),
            "access": str(access),
            "user": UserProfileSerializer(user).data,
            "active_business": serialize_active_business(
                business,
                access_mode,
            ),
        }


class VGSyncTokenRefreshSerializer(TokenRefreshSerializer):
    """
    Revalidates the current user and active business before
    issuing a new access token from an existing refresh token.
    """

    def validate(self, attrs):
        # 1. Validate refresh token signature, expiry and token type.
        refresh = self.token_class(attrs["refresh"])

        # 2. Retrieve current user from the database.
        user_id = refresh.get(api_settings.USER_ID_CLAIM)

        if not user_id:
            raise InvalidToken("Invalid refresh token.")

        user = (
            User.objects
            .select_related(
                "company",
                "business",
                "position",
                "position__business",
            )
            .filter(
                **{api_settings.USER_ID_FIELD: user_id}
            )
            .first()
        )

        # 3. User must still exist and be active.
        if (
            not user
            or not user.is_active
            or user.role == User.Role.SUPER_ADMIN
        ):
            raise InvalidToken(
                "This account is no longer allowed to refresh tokens."
            )

        # 4. Validate active business context.
        active_business_id = refresh.get("active_business_id")

        if not active_business_id:
            raise InvalidToken(
                "Active business context is missing."
            )

        business = (
            Business.objects
            .select_related("company")
            .filter(pk=active_business_id)
            .first()
        )

        # 5. Revalidate business, company, tenant assignment, role and
        #    Position against the current database state.
        try:
            access_mode = validate_user_access(user, business)
        except AccessDenied as exc:
            raise InvalidToken(exc.message)

        # 6. Update business context claims.
        refresh["active_business_code"] = business.business_code
        refresh["active_business_type"] = business.business_type
        refresh["active_company_id"] = business.company_id
        refresh["access_mode"] = access_mode

        # 7. Pass the validated token to SimpleJWT.
        attrs["refresh"] = str(refresh)

        return super().validate(attrs)


class BusinessContextSwitchSerializer(serializers.Serializer):
    refresh = serializers.CharField(write_only=True)
    business_id = serializers.IntegerField(min_value=1)

    @transaction.atomic
    def validate(self, attrs):
        request = self.context["request"]
        user = request.user

        # 1. Validate the supplied refresh token.
        try:
            current_refresh = RefreshToken(attrs["refresh"])
        except (TokenError, InvalidToken):
            raise serializers.ValidationError({
                "refresh": "Invalid, expired or revoked refresh token."
            })

        # 2. The refresh token must belong to the authenticated user.
        token_user_id = current_refresh.get(api_settings.USER_ID_CLAIM)

        if str(token_user_id) != str(user.pk):
            raise serializers.ValidationError({
                "refresh": (
                    "This refresh token does not belong to your account."
                )
            })

        # 3. The refresh token must match the current business context.
        token_business_id = current_refresh.get("active_business_id")

        if str(token_business_id) != str(request.active_business.pk):
            raise serializers.ValidationError({
                "refresh": (
                    "The refresh token does not match your current "
                    "business context. Please log in again."
                )
            })

        # 4. Fetch the target business.
        business = (
            Business.objects
            .select_related("company")
            .filter(pk=attrs["business_id"])
            .first()
        )

        if not business:
            raise serializers.ValidationError({
                "business_id": "The selected business is unavailable."
            })

        # 5. Revalidate tenant, role, Position and business access.
        try:
            access_mode = validate_user_access(user, business)
        except AccessDenied as exc:
            raise serializers.ValidationError({
                "business_id": exc.message
            })

        # 6. Revoke the current refresh token.
        current_refresh.blacklist()

        # 7. Issue a new refresh token with the target business context.
        refresh = VGSyncTokenObtainPairSerializer.get_token(user)

        refresh["active_business_id"] = business.id
        refresh["active_business_code"] = business.business_code
        refresh["active_business_type"] = business.business_type
        refresh["active_company_id"] = business.company_id
        refresh["access_mode"] = access_mode

        access = refresh.access_token

        return {
            "refresh": str(refresh),
            "access": str(access),
            "user": UserProfileSerializer(user).data,
            "active_business": serialize_active_business(
                business,
                access_mode,
            ),
        }


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(
        write_only=True,
        trim_whitespace=False,
    )

    new_password = serializers.CharField(
        write_only=True,
        trim_whitespace=False,
    )

    confirm_password = serializers.CharField(
        write_only=True,
        trim_whitespace=False,
    )

    def validate(self, attrs):
        user = self.context["request"].user

        if not user.check_password(attrs["current_password"]):
            raise serializers.ValidationError({
                "current_password": "Current password is incorrect."
            })

        if attrs["new_password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({
                "confirm_password": "Passwords do not match."
            })

        # Otherwise a user forced to change a temporary password could
        # clear the flag by "changing" it to the same value.
        if attrs["new_password"] == attrs["current_password"]:
            raise serializers.ValidationError({
                "new_password": (
                    "The new password must be different from the "
                    "current password."
                )
            })

        validate_password(attrs["new_password"], user=user)

        return attrs

    def save(self, **kwargs):
        user = kwargs["user"]

        with transaction.atomic():
            user.set_password(self.validated_data["new_password"])
            user.must_change_password = False

            user.save(update_fields=[
                "password",
                "must_change_password",
            ])

            # A password change signs the user out everywhere: every
            # existing refresh token is revoked and they must log in again.
            revoke_all_refresh_tokens(user)

        return user


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()

    def save(self, **kwargs):
        try:
            RefreshToken(self.validated_data["refresh"]).blacklist()
        except TokenError:
            # Expired, malformed or already blacklisted: there is nothing
            # left to revoke, so logging out again is not an error.
            pass


class ForgotPasswordSerializer(serializers.Serializer):
    """
    Starts a password reset. It never says whether the account exists or
    has an email address, so it cannot be used to discover usernames.
    """

    business_code = serializers.CharField(max_length=50)
    username = serializers.CharField(max_length=150)

    def _find_user(self):
        business_code = self.validated_data["business_code"].strip()
        username = self.validated_data["username"].strip()

        business = (
            Business.objects
            .select_related("company")
            .filter(business_code__iexact=business_code, is_active=True)
            .first()
        )

        if not business:
            return None

        if business.company_id and not business.company.is_active:
            return None

        users = (
            User.objects
            .filter(username=username, is_active=True)
            .exclude(role=User.Role.SUPER_ADMIN)
        )

        if business.company_id:
            return users.filter(tenant_company_id=business.company_id).first()

        return users.filter(
            business_id=business.id,
            tenant_company__isnull=True,
        ).first()

    def save(self, **kwargs):
        user = self._find_user()

        if not user or not user.email:
            return False

        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        business_code = self.validated_data["business_code"].strip()
        link = (
            f"{settings.FRONTEND_URL}/reset-password"
            f"?uid={uid}&token={token}"
        )
        minutes = max(settings.PASSWORD_RESET_TIMEOUT // 60, 1)

        send_mail(
            subject="Reset your VGSync password",
            message=(
                f"Hello {user.first_name or user.username},\n\n"
                f"We received a request to reset the password for "
                f"{user.username} ({business_code}).\n\n"
                f"Open this link to choose a new password "
                f"(valid for {minutes} minutes, one use only):\n{link}\n\n"
                f"If you did not ask for this, ignore this email; your "
                f"password stays the same."
            ),
            from_email=None,
            recipient_list=[user.email],
            fail_silently=True,
        )

        return True


class ResetPasswordSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)
    confirm_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        invalid = serializers.ValidationError({
            "detail": "This reset link is invalid or has expired."
        })

        try:
            user_id = force_str(urlsafe_base64_decode(attrs["uid"]))
            user = User.objects.get(pk=user_id, is_active=True)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            raise invalid

        if user.role == User.Role.SUPER_ADMIN:
            raise invalid

        if not default_token_generator.check_token(user, attrs["token"]):
            raise invalid

        if attrs["new_password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({
                "confirm_password": "Passwords do not match."
            })

        validate_password(attrs["new_password"], user=user)

        attrs["user"] = user

        return attrs

    def save(self, **kwargs):
        user = self.validated_data["user"]

        with transaction.atomic():
            user.set_password(self.validated_data["new_password"])
            user.must_change_password = False
            user.save(update_fields=["password", "must_change_password"])

            # Signs the user out everywhere; also makes this link single-use
            # because the token depends on the password hash.
            revoke_all_refresh_tokens(user)

        return user
