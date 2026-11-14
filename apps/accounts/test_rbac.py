"""Permission resolver: positions, overrides, reserved codes, read-only."""

from rest_framework.test import APITestCase

from apps.accounts import rbac
from apps.accounts.models import StaffPermissionOverride, User
from apps.common.testing import TenantTestMixin

STAFF = "/api/v1/staff/"
POSITIONS = "/api/v1/positions/"
EXPENSES = "/api/v1/finance/expenses/"


class ResolverTests(TenantTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.position = self.make_position(self.ho, "Sales", sales=("view", "create"))
        self.clerk = self.make_user(
            "clerk", User.Role.STAFF, company=self.company, position=self.position
        )

    def has(self, code, mode="full"):
        user = User.objects.get(pk=self.clerk.pk)

        return rbac.has_permission(user, code, mode)

    def override(self, code, effect):
        StaffPermissionOverride.objects.create(
            user=self.clerk, permission=code, effect=effect
        )

    def test_the_position_decides_when_there_is_no_override(self):
        self.assertTrue(self.has("sales.create"))
        self.assertFalse(self.has("sales.cancel"))
        self.assertFalse(self.has("finance.view"))

    def test_an_override_can_allow_or_deny_one_permission(self):
        self.override("sales.cancel", "allow")
        self.override("sales.create", "deny")

        self.assertTrue(self.has("sales.cancel"))
        self.assertFalse(self.has("sales.create"))
        self.assertTrue(self.has("sales.view"))

    def test_nobody_but_the_owner_holds_a_reserved_permission(self):
        self.override("staff.manage_permissions", "allow")
        rbac.write_position_codes(self.position, {"staff.manage_permissions"})

        self.assertFalse(self.has("staff.manage_permissions"))
        self.assertTrue(
            rbac.has_permission(self.owner, "staff.manage_permissions")
        )

    def test_a_branch_view_keeps_only_reading(self):
        owner = User.objects.get(pk=self.owner.pk)

        self.assertTrue(rbac.has_permission(owner, "sales.create", "full"))
        self.assertFalse(rbac.has_permission(owner, "sales.create", "read_only"))
        self.assertTrue(rbac.has_permission(owner, "sales.view", "read_only"))

    def test_an_inactive_position_grants_nothing(self):
        self.position.is_active = False
        self.position.save()

        self.assertFalse(self.has("sales.view"))

    def test_a_change_applies_on_the_very_next_request(self):
        self.authenticate("ACME-HO", "clerk")
        url = "/api/v1/sales/"
        before = self.client.get(url).status_code

        self.position.permissions.filter(module="sales").update(can_view=False)

        self.assertEqual(before, 200)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_every_preset_uses_only_known_non_reserved_codes(self):
        for key in rbac.PRESETS:
            codes = rbac.preset_codes(key)

            self.assertTrue(codes <= rbac.delegable_codes(), key)


class TechnicianPresetTests(TenantTestMixin, APITestCase):
    """A technician sees stock and prices and his own work, and nothing else."""

    def setUp(self):
        super().setUp()
        self.assertEqual(rbac.preset_codes("technician"), {"inventory.view"})
        position = self.make_position(self.ho, "Technician", inventory=("view",))
        self.tech = self.make_user(
            "tech", User.Role.STAFF, company=self.company, position=position
        )
        self.authenticate("ACME-HO", "tech")

    def test_he_can_look_at_stock_and_prices(self):
        response = self.client.get("/api/v1/inventory/products/")

        self.assertEqual(response.status_code, 200)

    def test_he_cannot_change_stock_or_see_sales_money_or_staff(self):
        self.assertEqual(
            self.client.post("/api/v1/inventory/products/", {}, format="json").status_code, 403
        )
        for url in ("/api/v1/sales/", "/api/v1/finance/income/", "/api/v1/staff/"):
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_he_sees_his_own_pay_and_tasks(self):
        # 404 only means no salary is set up for him yet; it is not a refusal.
        self.assertIn(self.client.get("/api/v1/payroll/me/").status_code, (200, 404))
        self.assertEqual(self.client.get("/api/v1/tasks/?mine=true").status_code, 200)


class AccessApiTests(TenantTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.position = self.make_position(self.ho, "Sales", sales=("view",))
        self.clerk = self.make_user(
            "clerk", User.Role.STAFF, company=self.company, position=self.position
        )

    def put(self, user, overrides):
        return self.client.put(
            f"{STAFF}{user.pk}/access/", {"overrides": overrides}, format="json"
        )

    def test_the_owner_sets_and_clears_an_override(self):
        self.authenticate("ACME-HO", "owner")

        response = self.put(
            self.clerk, [{"permission": "sales.cancel", "effect": "allow"}]
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("sales.cancel", response.data["effective"])

        response = self.put(
            self.clerk, [{"permission": "sales.cancel", "effect": "inherit"}]
        )
        self.assertNotIn("sales.cancel", response.data["effective"])

    def test_reserved_and_unknown_permissions_are_refused(self):
        self.authenticate("ACME-HO", "owner")

        for code in ("staff.manage_permissions", "nope.nothing"):
            response = self.put(self.clerk, [{"permission": code, "effect": "allow"}])
            self.assertEqual(response.status_code, 400, code)

    def test_staff_cannot_change_overrides_even_their_own(self):
        rbac.write_position_codes(
            self.position, {"staff.view", "staff.update", "staff.assign_position"}
        )
        self.authenticate("ACME-HO", "clerk")

        response = self.put(
            self.clerk, [{"permission": "finance.view", "effect": "allow"}]
        )

        self.assertIn(response.status_code, (403, 404))
        self.assertFalse(self.clerk.permission_overrides.exists())

    def test_a_new_position_starts_clean(self):
        other = self.make_position(self.ho, "Cash", finance=("view",))
        StaffPermissionOverride.objects.create(
            user=self.clerk, permission="sales.cancel", effect="allow"
        )
        self.authenticate("ACME-HO", "owner")

        response = self.client.patch(
            f"{STAFF}{self.clerk.pk}/", {"position": other.pk}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(self.clerk.permission_overrides.exists())

    def test_presets_and_catalogue_are_served(self):
        self.authenticate("ACME-HO", "owner")

        presets = self.client.get(f"{POSITIONS}presets/")
        catalogue = self.client.get(f"{POSITIONS}catalogue/")

        self.assertEqual(presets.status_code, 200)
        self.assertIn("accountant", [p["key"] for p in presets.data])
        self.assertEqual(catalogue.status_code, 200)

    def test_an_accountant_position_reaches_finance_but_not_payroll(self):
        position = self.make_position(self.ho, "Accountant")
        rbac.write_position_codes(position, rbac.preset_codes("accountant"))
        self.make_user(
            "acc", User.Role.STAFF, company=self.company, position=position
        )
        self.authenticate("ACME-HO", "acc")

        self.assertEqual(self.client.get(EXPENSES).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/payroll/salaries/").status_code, 403)
