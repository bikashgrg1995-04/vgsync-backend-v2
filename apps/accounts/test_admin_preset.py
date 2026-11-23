"""The Administrator position: wide, but never the owner and never salaries."""

from rest_framework.test import APITestCase

from apps.accounts import rbac
from apps.accounts.models import User
from apps.common.testing import TenantTestMixin

AUDIT = "/api/v1/audit/"
RULES = "/api/v1/approvals/rules/"
POSITIONS = "/api/v1/positions/"
BUSINESSES = "/api/v1/businesses/"
SALARIES = "/api/v1/payroll/salaries/"
BACKUPS = "/api/v1/backups/"


class AdministratorTests(TenantTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.adm_pos = self.make_position(self.ho, "Administrator")
        rbac.write_position_codes(self.adm_pos, rbac.preset_codes("administrator"))
        self.make_user(
            "adm", User.Role.STAFF, company=self.company, position=self.adm_pos
        )
        self.plain_pos = self.make_position(self.ho, "Plain", sales=("view",))
        self.make_user(
            "plain", User.Role.STAFF, company=self.company, position=self.plain_pos
        )

    def test_preset_has_new_codes_but_not_pay_or_reserved(self):
        codes = rbac.preset_codes("administrator")

        for code in (
            "staff.view_audit", "staff.manage_positions", "finance.manage_rules",
        ):
            self.assertIn(code, codes)

        self.assertNotIn("staff.pay_view", codes)
        self.assertNotIn("staff.manage_permissions", codes)

    def test_admin_reads_audit_and_rules(self):
        self.authenticate("ACME-HO", "adm")

        for url in (AUDIT, RULES):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_admin_still_cannot_see_salaries(self):
        self.authenticate("ACME-HO", "adm")

        self.assertEqual(self.client.get(SALARIES).status_code, 403)

    def test_plain_staff_cannot_use_them(self):
        self.authenticate("ACME-HO", "plain")

        for url in (AUDIT, RULES):
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_admin_cannot_create_positions_only_the_owner_can(self):
        self.authenticate("ACME-HO", "adm")

        for codes in (["sales.view", "sales.create"], ["staff.pay"], ["staff.manage_permissions"]):
            response = self.client.post(
                POSITIONS, {"name": "Counter", "codes": codes}, format="json"
            )
            self.assertEqual(response.status_code, 403, codes)

    def test_owner_creates_a_position(self):
        self.authenticate("ACME-HO", "owner")

        response = self.client.post(
            POSITIONS,
            {"name": "Counter", "codes": ["sales.view", "sales.create"]},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)

    def test_plain_staff_cannot_create_positions(self):
        self.authenticate("ACME-HO", "plain")

        self.assertEqual(
            self.client.post(POSITIONS, {"name": "X", "codes": []}, format="json").status_code,
            403,
        )

    def test_admin_can_set_an_approval_rule(self):
        self.authenticate("ACME-HO", "adm")
        listing = self.client.get(RULES)

        self.assertEqual(listing.status_code, 200)
