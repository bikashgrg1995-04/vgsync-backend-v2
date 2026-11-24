"""Approvals are switched off: nothing is held and the old routes are gone."""

from rest_framework.test import APITestCase

from apps.accounts import rbac
from apps.accounts.models import User
from apps.approvals.models import ApprovalRule, Kind
from apps.common.testing import TenantTestMixin

EXPENSES = "/api/v1/finance/expenses/"


class ApprovalsAreOffTests(TenantTestMixin, APITestCase):
    def setUp(self):
        super().setUp()

        position = self.make_position(self.ho, "Clerk")
        rbac.write_position_codes(position, {"finance.view", "finance.create"})
        self.make_user(
            "clerk", User.Role.STAFF, company=self.company, position=position
        )

    def test_a_large_expense_by_staff_is_saved_even_with_an_old_rule(self):
        ApprovalRule.objects.create(
            business=self.ho, kind=Kind.EXPENSE, threshold="1000"
        )
        self.authenticate("ACME-HO", "clerk")

        response = self.client.post(
            EXPENSES, {"category": "rent", "amount": "50000"}, format="json"
        )

        self.assertEqual(response.status_code, 201, response.data)

    def test_the_approval_routes_are_gone(self):
        self.authenticate("ACME-HO", "owner")

        self.assertEqual(self.client.get("/api/v1/approvals/").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/approvals/rules/").status_code, 404)

    def test_the_catalogue_has_no_approve_permission(self):
        codes = {
            f"{module}.{action}"
            for module, actions in rbac.CATALOG.items()
            for action in actions
        }

        self.assertFalse({c for c in codes if c.endswith(".approve")})
        self.assertNotIn("finance.manage_rules", codes)
