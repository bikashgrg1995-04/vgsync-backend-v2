
from types import SimpleNamespace

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import Position, PositionPermission, User
from apps.businesses.models import Business
from apps.companies.models import Company
from apps.common.permissions import (
    HasActiveBusinessContext,
    HasModulePermission,
)


class ModulePermissionTests(TestCase):

    def setUp(self):
        self.company = Company.objects.create(
            name="Permission Test Company",
            code="PERMISSION-TEST",
        )

        self.head_office = Business.objects.create(
            name="Permission Test Head Office",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=self.company,
            business_code="PERMISSION-HO",
        )

        self.branch = Business.objects.create(
            name="Permission Test Branch",
            business_type=Business.BusinessType.BRANCH,
            company=self.company,
            parent_business=self.head_office,
            business_code="PERMISSION-BRANCH",
        )

        self.position = Position.objects.create(
            business=self.head_office,
            name="Permission Test Staff",
        )

        self.staff = User.objects.create_user(
            username="permission-staff",
            password="TestPass123!",
            role=User.Role.STAFF,
            company=self.company,
            position=self.position,
        )

        self.company_owner = User.objects.create_user(
            username="permission-owner",
            password="TestPass123!",
            role=User.Role.OWNER,
            company=self.company,
        )

        self.permission = HasModulePermission()

    def make_request(
        self,
        user,
        method="GET",
        business=None,
        access_mode="full",
    ):
        return SimpleNamespace(
            user=user,
            method=method,
            active_business=business or self.head_office,
            access_mode=access_mode,
        )

    def make_view(self, module="inventory"):
        return SimpleNamespace(permission_module=module)

    def test_staff_without_module_permission_is_denied(self):
        request = self.make_request(self.staff)

        allowed = self.permission.has_permission(
            request,
            self.make_view("inventory"),
        )

        self.assertFalse(allowed)

    def test_staff_can_view_when_view_permission_is_enabled(self):
        PositionPermission.objects.create(
            position=self.position,
            module="inventory",
            can_view=True,
        )

        request = self.make_request(self.staff, method="GET")

        allowed = self.permission.has_permission(
            request,
            self.make_view("inventory"),
        )

        self.assertTrue(allowed)

    def test_view_only_staff_cannot_create_update_or_delete(self):
        PositionPermission.objects.create(
            position=self.position,
            module="inventory",
            can_view=True,
            can_create=False,
            can_update=False,
            can_delete=False,
        )

        for method in ["POST", "PUT", "PATCH", "DELETE"]:
            with self.subTest(method=method):
                request = self.make_request(
                    self.staff,
                    method=method,
                )

                allowed = self.permission.has_permission(
                    request,
                    self.make_view("inventory"),
                )

                self.assertFalse(allowed)

    def test_staff_with_inactive_position_is_denied(self):
        self.position.is_active = False
        self.position.save(update_fields=["is_active"])

        PositionPermission.objects.create(
            position=self.position,
            module="inventory",
            can_view=True,
        )

        request = self.make_request(self.staff)

        allowed = self.permission.has_permission(
            request,
            self.make_view("inventory"),
        )

        self.assertFalse(allowed)

    def test_company_owner_can_read_branch(self):
        request = self.make_request(
            self.company_owner,
            method="GET",
            business=self.branch,
            access_mode="read_only",
        )

        allowed = self.permission.has_permission(
            request,
            self.make_view("inventory"),
        )

        self.assertTrue(allowed)

    def test_company_owner_cannot_write_to_branch(self):
        for method in ["POST", "PUT", "PATCH", "DELETE"]:
            with self.subTest(method=method):
                request = self.make_request(
                    self.company_owner,
                    method=method,
                    business=self.branch,
                    access_mode="read_only",
                )

                allowed = self.permission.has_permission(
                    request,
                    self.make_view("inventory"),
                )

                self.assertFalse(allowed)

    def test_company_owner_has_full_head_office_access(self):
        for method in ["GET", "POST", "PUT", "PATCH", "DELETE"]:
            with self.subTest(method=method):
                request = self.make_request(
                    self.company_owner,
                    method=method,
                    business=self.head_office,
                    access_mode="full",
                )

                allowed = self.permission.has_permission(
                    request,
                    self.make_view("inventory"),
                )

                self.assertTrue(allowed)

    def test_missing_permission_module_is_denied(self):
        request = self.make_request(self.company_owner)
        view = SimpleNamespace()

        allowed = self.permission.has_permission(request, view)

        self.assertFalse(allowed)

    def test_missing_active_business_context_is_denied(self):
        request = SimpleNamespace(
            user=self.company_owner,
            method="GET",
            active_business=None,
            access_mode="full",
        )

        allowed = self.permission.has_permission(
            request,
            self.make_view("inventory"),
        )

        self.assertFalse(allowed)

    def test_active_business_context_permission(self):
        permission = HasActiveBusinessContext()

        valid_request = self.make_request(self.company_owner)

        self.assertTrue(
            permission.has_permission(valid_request, self.make_view())
        )

        invalid_request = self.make_request(
            self.company_owner,
            business=None,
        )
        invalid_request.active_business = None

        self.assertFalse(
            permission.has_permission(invalid_request, self.make_view())
        )


class SuperAdminClientAccessTests(TestCase):

    def test_super_admin_cannot_access_client_api(self):
        super_admin = User.objects.create_superuser(
            username="permission-super-admin",
            email="superadmin@example.com",
            password="TestPass123!",
        )

        refresh = RefreshToken.for_user(super_admin)
        access_token = str(refresh.access_token)

        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {access_token}"
        )

        response = client.get("/api/v1/auth/me/")

        self.assertEqual(response.status_code, 401)
