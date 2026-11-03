from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from apps.common.testing import TenantTestMixin
from apps.finance.models import Expense, Income
from apps.sales.test_sales import SaleTestCase

EXPENSES = "/api/v1/finance/expenses/"
INCOME = "/api/v1/finance/income/"


class FinanceTestCase(TenantTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate("ACME-HO", "owner")

    def spend(self, amount="500.00", **extra):
        return self.client.post(
            EXPENSES,
            {"category": "rent", "amount": amount, **extra},
            format="json",
        )

    def earn(self, amount="300.00", **extra):
        return self.client.post(
            INCOME,
            {"category": "other", "amount": amount, **extra},
            format="json",
        )


class ExpenseTests(FinanceTestCase):
    def test_owner_adds_an_expense_dated_today_by_default(self):
        response = self.spend(description="October rent")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["amount"], "500.00")
        self.assertEqual(response.data["status"], "active")
        self.assertEqual(
            response.data["expense_date"], str(timezone.localdate())
        )

    def test_salary_and_purchase_categories_are_refused(self):
        for category in ("salary", "purchase"):
            response = self.client.post(
                EXPENSES, {"category": category, "amount": "10"}, format="json"
            )
            self.assertEqual(response.status_code, 400, category)

    def test_bad_amounts_and_future_dates_are_refused(self):
        tomorrow = str(timezone.localdate() + timedelta(days=1))

        for payload in (
            {"amount": "0"},
            {"amount": "-5"},
            {"amount": "abc"},
            {"expense_date": tomorrow},
        ):
            data = {"category": "rent", "amount": "10", **payload}
            self.assertEqual(
                self.client.post(EXPENSES, data, format="json").status_code,
                400,
                payload,
            )

    def test_an_expense_can_be_edited_but_not_deleted(self):
        expense = self.spend().json()

        edited = self.client.patch(
            f"{EXPENSES}{expense['id']}/", {"amount": "650.00"}, format="json"
        )
        deleted = self.client.delete(f"{EXPENSES}{expense['id']}/")

        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.data["amount"], "650.00")
        self.assertEqual(deleted.status_code, 405)

    def test_voiding_needs_a_reason_and_works_once(self):
        expense = self.spend().json()
        url = f"{EXPENSES}{expense['id']}/void/"

        self.assertEqual(
            self.client.post(url, {}, format="json").status_code, 400
        )

        voided = self.client.post(url, {"reason": "Typo"}, format="json")
        again = self.client.post(url, {"reason": "Typo"}, format="json")

        self.assertEqual(voided.status_code, 200)
        self.assertEqual(voided.data["status"], "void")
        self.assertEqual(voided.data["void_reason"], "Typo")
        self.assertEqual(again.status_code, 409)

    def test_a_void_expense_cannot_be_edited(self):
        expense = self.spend().json()
        self.client.post(
            f"{EXPENSES}{expense['id']}/void/", {"reason": "x"}, format="json"
        )

        response = self.client.patch(
            f"{EXPENSES}{expense['id']}/", {"amount": "1"}, format="json"
        )

        self.assertEqual(response.status_code, 409)

    def test_only_the_owner_can_use_expenses(self):
        self.client.credentials()

        self.assertEqual(self.client.get(EXPENSES).status_code, 401)

    def test_expenses_are_scoped_to_the_active_business(self):
        self.spend()
        Expense.objects.create(
            business=self.other_ho, category="rent", amount=Decimal("99"),
            expense_date=timezone.localdate(),
        )

        listed = self.client.get(EXPENSES).json()

        self.assertEqual(listed["count"], 1)

    def test_filters_and_summary_by_category(self):
        self.spend("500.00", category="rent")
        self.spend("200.00", category="utilities")
        self.spend("100.00", category="utilities")
        gone = self.spend("999.00", category="rent").json()
        self.client.post(
            f"{EXPENSES}{gone['id']}/void/", {"reason": "dup"}, format="json"
        )

        only_rent = self.client.get(EXPENSES, {"category": "rent"}).json()
        summary = self.client.get(f"{EXPENSES}summary/").json()

        self.assertEqual(only_rent["count"], 2)
        self.assertEqual(summary["total"], "800.00")
        self.assertEqual(summary["net_total"], "800.00")
        by = {row["category"]: row for row in summary["by_category"]}
        self.assertEqual(by["utilities"]["total"], "300.00")
        self.assertEqual(by["utilities"]["count"], 2)
        self.assertNotIn("999.00", str(summary))

    def test_summary_respects_the_date_range(self):
        today = timezone.localdate()
        old = str(today - timedelta(days=40))
        self.spend("100.00", expense_date=old)
        self.spend("50.00")

        recent = self.client.get(
            f"{EXPENSES}summary/", {"expense_date__gte": str(today)}
        ).json()

        self.assertEqual(recent["total"], "50.00")

    def test_a_bad_date_in_the_summary_is_refused_not_a_crash(self):
        self.spend("50.00")

        response = self.client.get(
            f"{EXPENSES}summary/", {"expense_date__gte": "2026-13-45"}
        )

        self.assertEqual(response.status_code, 400)


class IncomeTests(FinanceTestCase):
    def test_owner_adds_other_income(self):
        response = self.earn(description="Scrap sold")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["category"], "other")
        self.assertEqual(response.data["status"], "active")
        self.assertEqual(response.data["income_date"], str(timezone.localdate()))

    def test_sale_categories_cannot_be_entered_by_hand(self):
        for category in ("stock_sale", "bike_sale", "maintenance"):
            response = self.client.post(
                INCOME, {"category": category, "amount": "10"}, format="json"
            )
            self.assertEqual(response.status_code, 400, category)

    def test_bad_amounts_and_future_dates_are_refused(self):
        tomorrow = str(timezone.localdate() + timedelta(days=1))

        for payload in ({"amount": "0"}, {"amount": "-1"}, {"income_date": tomorrow}):
            data = {"category": "other", "amount": "10", **payload}
            self.assertEqual(
                self.client.post(INCOME, data, format="json").status_code, 400
            )

    def test_income_cannot_be_edited_or_deleted(self):
        income = self.earn().json()
        url = f"{INCOME}{income['id']}/"

        self.assertEqual(
            self.client.patch(url, {"amount": "1"}, format="json").status_code, 405
        )
        self.assertEqual(self.client.delete(url).status_code, 405)

    def test_voiding_needs_a_reason_and_works_once(self):
        income = self.earn().json()
        url = f"{INCOME}{income['id']}/void/"

        self.assertEqual(self.client.post(url, {}, format="json").status_code, 400)
        self.assertEqual(
            self.client.post(url, {"reason": "Wrong"}, format="json").status_code, 200
        )
        self.assertEqual(
            self.client.post(url, {"reason": "Wrong"}, format="json").status_code, 409
        )

    def test_income_is_scoped_to_the_active_business(self):
        self.earn()
        Income.objects.create(
            business=self.other_ho, category="other", amount=Decimal("9"),
            income_date=timezone.localdate(),
        )

        self.assertEqual(self.client.get(INCOME).json()["count"], 1)

    def test_summary_leaves_out_void_income(self):
        self.earn("300.00")
        gone = self.earn("700.00").json()
        self.client.post(
            f"{INCOME}{gone['id']}/void/", {"reason": "x"}, format="json"
        )

        summary = self.client.get(f"{INCOME}summary/").json()

        self.assertEqual(summary["total"], "300.00")
        self.assertEqual(summary["net_total"], "300.00")
        self.assertEqual(summary["sale_refunds"], "0.00")

    def test_a_bad_date_in_the_summary_is_refused_not_a_crash(self):
        self.assertEqual(
            self.client.get(f"{INCOME}summary/", {"income_date__lte": "2026-02-31"}).status_code,
            400,
        )


class SaleIncomeTests(SaleTestCase):
    """Income that comes from customer payments."""

    def test_a_payment_creates_income_in_the_sale_category(self):
        sale = self.confirmed()
        self.assertEqual(self.pay(sale["id"], "400.00").status_code, 201)

        listed = self.client.get(INCOME).json()

        self.assertEqual(listed["count"], 1)
        row = listed["results"][0]
        self.assertEqual(row["category"], "stock_sale")
        self.assertEqual(row["amount"], "400.00")
        self.assertIsNotNone(row["sale_payment"])

    def test_income_from_a_payment_cannot_be_voided_directly(self):
        sale = self.confirmed()
        self.pay(sale["id"], "400.00")
        income = self.client.get(INCOME).json()["results"][0]

        response = self.client.post(
            f"{INCOME}{income['id']}/void/", {"reason": "x"}, format="json"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "payment_income_locked")

    def test_voiding_the_payment_removes_its_income_from_the_summary(self):
        sale = self.confirmed()
        payment = self.pay(sale["id"], "400.00").json()
        self.pay(sale["id"], "100.00")

        self.client.post(
            f"/api/v1/sales/payments/{payment['id']}/void/",
            {"reason": "Bounced"},
            format="json",
        )

        summary = self.client.get(f"{INCOME}summary/").json()
        self.assertEqual(summary["total"], "100.00")
