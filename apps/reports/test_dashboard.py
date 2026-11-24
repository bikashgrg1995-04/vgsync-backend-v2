from datetime import date

from rest_framework.test import APITestCase

from apps.accounts import rbac
from apps.accounts.models import User
from apps.common.testing import TenantTestMixin
from apps.finance.models import Expense, Income

URL = "/api/v1/reports/dashboard/"


class DashboardTests(TenantTestMixin, APITestCase):
    def test_get_answers_carry_an_etag_and_unchanged_data_gets_304(self):
        # The web app keeps a local copy and asks "did it change?" with If-None-Match.
        self.authenticate("ACME-HO", "owner")
        first = self.client.get(URL)
        etag = first["ETag"]
        self.assertTrue(etag)
        again = self.client.get(URL, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.content, b"")

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

    def test_the_opening_balance_is_part_of_cash_and_bank(self):
        from datetime import timedelta

        from apps.finance.models import OpeningBalance

        today = date.today()
        OpeningBalance.objects.create(
            business=self.ho, method="cash", amount="5000", as_of=today - timedelta(days=2)
        )
        Income.objects.create(
            business=self.ho, category="other", amount="1000",
            income_date=today - timedelta(days=9), payment_method="cash",
        )
        Income.objects.create(
            business=self.ho, category="other", amount="300",
            income_date=today, payment_method="cash",
        )
        Income.objects.create(
            business=self.ho, category="other", amount="40", income_date=today,
        )
        self.authenticate("ACME-HO", "owner")

        data = self.client.get(URL).data["cash"]

        # 1000 was before the opening date, so it is already inside the 5000.
        self.assertEqual((data["cash"], data["other"], data["total"]), ("5300.00", "40.00", "5340.00"))

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


class DashboardOverviewTests(TenantTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate("ACME-HO", "owner")

    def test_overview_blocks_and_range(self):
        data = self.client.get(URL, {"days": "7"}).json()

        self.assertEqual(data["overview"]["days"], 7)

        for name in ("sales", "purchases", "income", "expenses"):
            self.assertEqual(len(data["overview"][name]["series"]), 7)
            self.assertIn("change_pct", data["overview"][name])

        self.assertEqual(len(data["overview"]["labels"]), 7)
        self.assertEqual(self.client.get(URL, {"days": "5"}).json()["overview"]["days"], 30)

    def test_due_lists_and_activity_exist(self):
        data = self.client.get(URL).json()

        for key in ("installments", "purchase_credit", "sales_credit"):
            self.assertIn("amount" if "credit" in key else "count", data["due"][key])

        self.assertIn("count", data["due"]["servicing"])
        self.assertIsInstance(data["installments"], list)
        self.assertIsInstance(data["servicing"], list)
        self.assertIsInstance(data["activity"], list)
