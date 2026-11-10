from datetime import date

from rest_framework.test import APITestCase

from apps.accounts import rbac
from apps.accounts.models import User
from apps.approvals.models import ApprovalRequest, Kind
from apps.common.testing import TenantTestMixin
from apps.finance.models import Expense, Income

URL = "/api/v1/reports/dashboard/"


class DashboardTests(TenantTestMixin, APITestCase):
    def test_an_empty_business_returns_zeros_not_errors(self):
        self.authenticate("ACME-HO", "owner")

        data = self.client.get(URL).data

        self.assertEqual(data["sales_today"], "0.00")
        self.assertEqual(data["receivables"], "0.00")
        self.assertEqual(data["cash"]["total"], "0.00")
        self.assertEqual(data["alerts"], [])
        self.assertEqual(data["sales_trend"], [])

    def test_cash_and_bank_follow_income_less_expenses(self):
        today = date.today()
        Income.objects.create(
            business=self.ho, category="other", amount="1000",
            income_date=today, payment_method="cash",
        )
        Income.objects.create(
            business=self.ho, category="other", amount="500",
            income_date=today, payment_method="bank",
        )
        Expense.objects.create(
            business=self.ho, category="rent", amount="200",
            expense_date=today, payment_method="cash",
        )
        self.authenticate("ACME-HO", "owner")

        data = self.client.get(URL).data

        self.assertEqual(data["cash"]["cash"], "800.00")
        self.assertEqual(data["cash"]["bank"], "500.00")
        self.assertEqual(data["income_month"], "1500.00")
        self.assertEqual(data["profit_month"], "1300.00")

    def test_pending_approvals_raise_an_alert(self):
        ApprovalRequest.objects.create(
            business=self.ho, kind=Kind.EXPENSE, amount="5000",
            payload_hash="x", created_by=self.owner,
        )
        self.authenticate("ACME-HO", "owner")

        data = self.client.get(URL).data

        self.assertEqual(data["approvals_pending"], 1)
        self.assertEqual(data["alerts"][0]["kind"], "approvals")

    def test_head_office_sees_branches_but_a_branch_does_not(self):
        self.authenticate("ACME-HO", "owner")
        head = self.client.get(URL).data

        self.assertEqual([b["name"] for b in head["branches"]], ["Acme Branch"])

        tokens = self.authenticate("ACME-HO", "owner")
        response = self.client.post(
            "/api/v1/auth/business-context/",
            {"refresh": tokens["refresh"], "business_id": self.branch.pk},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {response.data['access']}"
        )
        branch = self.client.get(URL).data

        self.assertEqual(branch["access_mode"], "read_only")
        self.assertNotIn("branches", branch)

    def test_data_never_crosses_businesses(self):
        Income.objects.create(
            business=self.other_ho, category="other", amount="9999",
            income_date=date.today(), payment_method="cash",
        )
        self.authenticate("ACME-HO", "owner")

        self.assertEqual(self.client.get(URL).data["cash"]["cash"], "0.00")

    def test_staff_need_dashboard_view(self):
        position = self.make_position(self.ho, "Sales", sales=("view",))
        self.make_user(
            "clerk", User.Role.STAFF, company=self.company, position=position
        )
        self.authenticate("ACME-HO", "clerk")
        self.assertEqual(self.client.get(URL).status_code, 403)

        rbac.write_position_codes(position, {"dashboard.view"})
        self.assertEqual(self.client.get(URL).status_code, 200)
