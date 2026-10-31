
from django.contrib.auth.password_validation import validate_password

from rest_framework import serializers

from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.serializers import (
    TokenObtainPairSerializer,
    TokenRefreshSerializer,
)
from rest_framework_simplejwt.settings import api_settings

from apps.businesses.models import Business

from .models import User


class UserProfileSerializer(serializers.ModelSerializer):
    scope = serializers.CharField(read_only=True)
    company_name = serializers.SerializerMethodField()
    business_name = serializers.SerializerMethodField()
    position_name = serializers.SerializerMethodField()

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
            "must_change_password",
        )
        read_only_fields = fields

    def get_company_name(self, obj):
        return obj.company.name if obj.company else None

    def get_business_name(self, obj):
        return obj.business.name if obj.business else None

    def get_position_name(self, obj):
        return obj.position.name if obj.position else None


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

        # 4. Validate the user's assigned scope.
        if user.company_id:

            if user.company_id != business.company_id:
                raise serializers.ValidationError(
                    {"detail": "You are not allowed to access this business."}
                )

            if user.role == User.Role.STAFF:
                head_office = Business.objects.filter(
                    company_id=user.company_id,
                    business_type=Business.BusinessType.HEAD_OFFICE,
                ).first()

                if not head_office or business.id != head_office.id:
                    raise serializers.ValidationError(
                        {"detail": "Company Staff can only access Head Office."}
                    )

            elif user.role not in [
                User.Role.OWNER,
                User.Role.ADMIN,
                User.Role.ACCOUNTANT,
            ]:
                raise serializers.ValidationError(
                    {"detail": "You are not allowed to access this business."}
                )

        elif user.business_id:

            if user.business_id != business.id:
                raise serializers.ValidationError(
                    {"detail": "You are not allowed to access this business."}
                )

        else:
            raise serializers.ValidationError(
                {"detail": "Invalid business code or login credentials."}
            )

        # 5. Staff must have an active, correctly scoped Position.
        if user.role == User.Role.STAFF:

            if not user.position_id or not user.position.is_active:
                raise serializers.ValidationError(
                    {"detail": "Your account does not have an active position."}
                )

            expected_position_business_id = (
                user.business_id
                if user.business_id
                else Business.objects.filter(
                    company_id=user.company_id,
                    business_type=Business.BusinessType.HEAD_OFFICE,
                ).values_list("id", flat=True).first()
            )

            if user.position.business_id != expected_position_business_id:
                raise serializers.ValidationError(
                    {"detail": "Your assigned position is invalid."}
                )

        # 6. Determine access mode.
        access_mode = "full"

        if (
            user.company_id
            and business.business_type == Business.BusinessType.BRANCH
        ):
            access_mode = "read_only"

        # 7. Generate JWT with selected business context.
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
            .filter(
                pk=active_business_id,
                is_active=True,
            )
            .first()
        )

        if not business:
            raise InvalidToken(
                "The selected business is unavailable."
            )

        # 5. Company must remain active.
        if business.company_id and not business.company.is_active:
            raise InvalidToken(
                "The selected company is inactive."
            )

        # 6. Revalidate current tenant assignment.
        if user.company_id:

            if (
                user.company_id != business.company_id
                or user.tenant_company_id != user.company_id
            ):
                raise InvalidToken(
                    "You are no longer authorized for this business."
                )

            if user.role == User.Role.STAFF:

                if (
                    business.business_type
                    != Business.BusinessType.HEAD_OFFICE
                ):
                    raise InvalidToken(
                        "Company Staff can only access Head Office."
                    )

            elif user.role not in [
                User.Role.OWNER,
                User.Role.ADMIN,
                User.Role.ACCOUNTANT,
            ]:
                raise InvalidToken(
                    "Your current role is not authorized."
                )

        elif user.business_id:

            if (
                user.business_id != business.id
                or user.tenant_company_id != business.company_id
            ):
                raise InvalidToken(
                    "You are no longer authorized for this business."
                )

            if user.role not in [
                User.Role.OWNER,
                User.Role.ADMIN,
                User.Role.ACCOUNTANT,
                User.Role.STAFF,
            ]:
                raise InvalidToken(
                    "Your current role is not authorized."
                )

        else:
            raise InvalidToken(
                "Your account has no valid business assignment."
            )

        # 7. Staff must retain a valid active Position.
        if user.role == User.Role.STAFF:

            if not user.position_id or not user.position.is_active:
                raise InvalidToken(
                    "Your assigned position is no longer active."
                )

            expected_business_id = (
                user.business_id
                if user.business_id
                else business.id
            )

            if user.position.business_id != expected_business_id:
                raise InvalidToken(
                    "Your assigned position is invalid."
                )

        # 8. Recalculate access mode from current database state.
        access_mode = (
            "read_only"
            if (
                user.company_id
                and business.business_type == Business.BusinessType.BRANCH
            )
            else "full"
        )

        # 9. Update business context claims.
        refresh["active_business_code"] = business.business_code
        refresh["active_business_type"] = business.business_type
        refresh["active_company_id"] = business.company_id
        refresh["access_mode"] = access_mode

        # 10. Pass the validated token to SimpleJWT.
        attrs["refresh"] = str(refresh)

        return super().validate(attrs)


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

        validate_password(attrs["new_password"], user=user)

        return attrs

    def save(self, **kwargs):
        user = kwargs["user"]

        user.set_password(self.validated_data["new_password"])
        user.must_change_password = False

        user.save(update_fields=[
            "password",
            "must_change_password",
        ])

        return user
