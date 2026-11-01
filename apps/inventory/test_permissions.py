
from types import SimpleNamespace

from django.test import TestCase

from apps.accounts.models import Position, PositionPermission, User
from apps.businesses.models import Business
from apps.common.permissions import (
    HasActiveBusinessContext,
    HasModulePermission,
)
from apps.companies.models import Company


class InventoryModulePermissionTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Permission Test Company",
            code="PERM-CO",
        )

        cls.head_office = Business.objects.create(
            name="Permission Test HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=cls.company,
            business_code="PERM-HO",
        )

        cls.branch = Business.objects.create(
            name="Permission Test Branch",
            business_type=Business.BusinessType.BRANCH,
            company=cls.company,
            parent_business=cls.head_office,
            business_code="PERM-BR",
        )

        cls.independent = Business.objects.create(
            name="Permission Test Independent",
            business_type=Business.BusinessType.INDEPENDENT,
            business_code="PERM-IND",
        )

        cls.position = Position.objects.create(
            business=cls.independent,
            name="Inventory Staff",
        )

    def make_user(self, username, **kwargs):
        return User.objects.create_user(
            username=username,
            password="TestPass123!",
            **kwargs,
        )

    def make_request(self, user, method="GET", business=None,
                     access_mode="full"):
        return SimpleNamespace(
            user=user,
            method=method,
            active_business=business,
            access_mode=access_mode,
        )

    def make_view(self, module="inventory"):
        return SimpleNamespace(permission_module=module)

    def check_permission(self, user, method, business,
                         access_mode="full", module="inventory"):
        request = self.make_request(
            user,
            method,
            business,
            access_mode,
        )

        return HasModulePermission().has_permission(
            request,
            self.make_view(module),
        )

    # --------------------------------------------------
    # COMPANY USER PERMISSIONS
    # --------------------------------------------------

    def test_company_owner_has_full_access_at_head_office(self):
        user = self.make_user(
            "permission-company-owner",
            role=User.Role.OWNER,
            company=self.company,
        )

        for method in ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]:
            with self.subTest(method=method):
                self.assertTrue(
                    self.check_permission(
                        user,
                        method,
                        self.head_office,
                    )
                )

    def test_company_owner_has_read_only_access_at_branch(self):
        user = self.make_user(
            "permission-branch-owner",
            role=User.Role.OWNER,
            company=self.company,
        )

        for method in ["GET", "HEAD"]:
            with self.subTest(method=method):
                self.assertTrue(
                    self.check_permission(
                        user,
                        method,
                        self.branch,
                        access_mode="read_only",
                    )
                )

        for method in ["POST", "PUT", "PATCH", "DELETE"]:
            with self.subTest(method=method):
                self.assertFalse(
                    self.check_permission(
                        user,
                        method,
                        self.branch,
                        access_mode="read_only",
                    )
                )

    # --------------------------------------------------
    # BUSINESS USER PERMISSIONS
    # --------------------------------------------------

    def test_branch_admin_has_full_access_to_own_business(self):
        user = self.make_user(
            "permission-branch-admin",
            role=User.Role.ADMIN,
            business=self.branch,
        )

        for method in ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]:
            with self.subTest(method=method):
                self.assertTrue(
                    self.check_permission(
                        user,
                        method,
                        self.branch,
                    )
                )

    def test_independent_owner_has_full_access(self):
        user = self.make_user(
            "permission-independent-owner",
            role=User.Role.OWNER,
            business=self.independent,
        )

        for method in ["GET", "POST", "PUT", "PATCH", "DELETE"]:
            with self.subTest(method=method):
                self.assertTrue(
                    self.check_permission(
                        user,
                        method,
                        self.independent,
                    )
                )

    # --------------------------------------------------
    # STAFF POSITION PERMISSIONS
    # --------------------------------------------------

    def test_staff_can_view_inventory_when_view_permission_is_enabled(self):
        user = self.make_user(
            "permission-staff-view",
            role=User.Role.STAFF,
            business=self.independent,
            position=self.position,
        )

        PositionPermission.objects.create(
            position=self.position,
            module=PositionPermission.Module.INVENTORY,
            can_view=True,
        )

        self.assertTrue(
            self.check_permission(
                user,
                "GET",
                self.independent,
            )
        )

        self.assertTrue(
            self.check_permission(
                user,
                "HEAD",
                self.independent,
            )
        )

    def test_staff_cannot_view_inventory_without_view_permission(self):
        user = self.make_user(
            "permission-staff-no-view",
            role=User.Role.STAFF,
            business=self.independent,
            position=self.position,
        )

        PositionPermission.objects.create(
            position=self.position,
            module=PositionPermission.Module.INVENTORY,
            can_create=True,
        )

        self.assertFalse(
            self.check_permission(
                user,
                "GET",
                self.independent,
            )
        )

    def test_staff_create_permission_is_enforced(self):
        user = self.make_user(
            "permission-staff-create",
            role=User.Role.STAFF,
            business=self.independent,
            position=self.position,
        )

        PositionPermission.objects.create(
            position=self.position,
            module=PositionPermission.Module.INVENTORY,
            can_create=True,
        )

        self.assertTrue(
            self.check_permission(
                user,
                "POST",
                self.independent,
            )
        )

        self.assertFalse(
            self.check_permission(
                user,
                "GET",
                self.independent,
            )
        )

    def test_staff_update_permission_covers_put_and_patch(self):
        user = self.make_user(
            "permission-staff-update",
            role=User.Role.STAFF,
            business=self.independent,
            position=self.position,
        )

        PositionPermission.objects.create(
            position=self.position,
            module=PositionPermission.Module.INVENTORY,
            can_update=True,
        )

        for method in ["PUT", "PATCH"]:
            with self.subTest(method=method):
                self.assertTrue(
                    self.check_permission(
                        user,
                        method,
                        self.independent,
                    )
                )

        self.assertFalse(
            self.check_permission(
                user,
                "POST",
                self.independent,
            )
        )

    def test_staff_delete_permission_is_enforced(self):
        user = self.make_user(
            "permission-staff-delete",
            role=User.Role.STAFF,
            business=self.independent,
            position=self.position,
        )

        PositionPermission.objects.create(
            position=self.position,
            module=PositionPermission.Module.INVENTORY,
            can_delete=True,
        )

        self.assertTrue(
            self.check_permission(
                user,
                "DELETE",
                self.independent,
            )
        )

        self.assertFalse(
            self.check_permission(
                user,
                "POST",
                self.independent,
            )
        )

    def test_staff_without_position_is_denied(self):
        user = self.make_user(
            "permission-staff-no-position",
            role=User.Role.STAFF,
            business=self.independent,
        )

        self.assertFalse(
            self.check_permission(
                user,
                "GET",
                self.independent,
            )
        )

    def test_staff_with_inactive_position_is_denied(self):
        self.position.is_active = False
        self.position.save(update_fields=["is_active"])

        user = self.make_user(
            "permission-staff-inactive-position",
            role=User.Role.STAFF,
            business=self.independent,
            position=self.position,
        )

        PositionPermission.objects.create(
            position=self.position,
            module=PositionPermission.Module.INVENTORY,
            can_view=True,
        )

        self.assertFalse(
            self.check_permission(
                user,
                "GET",
                self.independent,
            )
        )

    # --------------------------------------------------
    # FAIL-CLOSED AND HTTP METHOD TESTS
    # --------------------------------------------------

    def test_missing_module_configuration_is_denied(self):
        user = self.make_user(
            "permission-no-module",
            role=User.Role.OWNER,
            business=self.independent,
        )

        self.assertFalse(
            self.check_permission(
                user,
                "GET",
                self.independent,
                module=None,
            )
        )

    def test_unknown_http_method_is_denied(self):
        user = self.make_user(
            "permission-unknown-method",
            role=User.Role.OWNER,
            business=self.independent,
        )

        self.assertFalse(
            self.check_permission(
                user,
                "TRACE",
                self.independent,
            )
        )

    def test_options_method_is_allowed_with_authenticated_context(self):
        user = self.make_user(
            "permission-options",
            role=User.Role.OWNER,
            business=self.independent,
        )

        self.assertTrue(
            self.check_permission(
                user,
                "OPTIONS",
                self.independent,
            )
        )

    def test_missing_active_business_context_is_denied(self):
        user = self.make_user(
            "permission-no-context",
            role=User.Role.OWNER,
            business=self.independent,
        )

        self.assertFalse(
            self.check_permission(
                user,
                "GET",
                None,
            )
        )


class ActiveBusinessContextPermissionTests(TestCase):

    def test_authenticated_user_with_business_context_is_allowed(self):
        user = User.objects.create_user(
            username="context-user",
            password="TestPass123!",
            role=User.Role.OWNER,
            business=Business.objects.create(
                name="Context Business",
                business_type=Business.BusinessType.INDEPENDENT,
                business_code="CONTEXT-IND",
            ),
        )

        request = SimpleNamespace(
            user=user,
            active_business=user.business,
        )

        self.assertTrue(
            HasActiveBusinessContext().has_permission(
                request,
                SimpleNamespace(),
            )
        )

    def test_authenticated_user_without_business_context_is_denied(self):
        user = User.objects.create_user(
            username="context-no-business",
            password="TestPass123!",
            role=User.Role.OWNER,
            business=Business.objects.create(
                name="No Context Business",
                business_type=Business.BusinessType.INDEPENDENT,
                business_code="NO-CONTEXT-IND",
            ),
        )

        request = SimpleNamespace(
            user=user,
            active_business=None,
        )

        self.assertFalse(
            HasActiveBusinessContext().has_permission(
                request,
                SimpleNamespace(),
            )
        )

    def test_anonymous_user_is_denied(self):
        from django.contrib.auth.models import AnonymousUser

        request = SimpleNamespace(
            user=AnonymousUser(),
            active_business=None,
        )

        self.assertFalse(
            HasActiveBusinessContext().has_permission(
                request,
                SimpleNamespace(),
            )
        )
