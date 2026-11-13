from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.accounts.models import User
from apps.finance.models import Expense, Income, Reconciliation
from apps.finance.test_finance import FinanceTestCase

BALANCES = "/api/v1/finance/balances/"
OPENING = "/api/v1/finance/opening-balances/"
CHECKS = "/api/v1/finance/reconciliations/"


class CashTests(FinanceTestCase):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()

    def row(self, method, **params):
        data = self.client.get(BALANCES, params).json()

        return next(m for m in data["methods"] if m["method"] == method)

    def test_expected_balance_is_opening_plus_in_minus_out(self):
        past = self.today - timedelta(days=10)
        self.client.put(f"{OPENING}cash/", {"amount": "1000", "as_of": str(past)}, format="json")
        self.earn("300", payment_method="cash")
        self.spend("120", payment_method="cash")
        self.earn("999", payment_method="bank")

        cash = self.row("cash")
        self.assertEqual(
            (Decimal(cash["received"]), Decimal(cash["paid"]), Decimal(cash["expected"])),
            (Decimal("300"), Decimal("120"), Decimal("1180")),
        )
        self.assertEqual(Decimal(self.row("bank")["expected"]), Decimal("999"))

    def test_entries_on_or_before_the_opening_date_are_already_in_it(self):
        self.earn("300", payment_method="cash", income_date=str(self.today - timedelta(days=5)))
        self.client.put(
            f"{OPENING}cash/",
            {"amount": "500", "as_of": str(self.today - timedelta(days=5))}, format="json",
        )

        self.assertEqual(Decimal(self.row("cash")["expected"]), Decimal("500"))

    def test_voided_entries_do_not_count(self):
        entry = self.earn("300", payment_method="cash").json()
        self.client.post(f"{INCOME_URL}{entry['id']}/void/", {"reason": "typo"}, format="json")

        self.assertEqual(Decimal(self.row("cash")["expected"]), 0)

    def test_a_check_records_the_gap(self):
        self.earn("300", payment_method="cash")

        response = self.client.post(
            CHECKS, {"method": "cash", "counted": "250", "note": "drawer"}, format="json"
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            (Decimal(response.data["expected"]), Decimal(response.data["difference"])),
            (Decimal("300"), Decimal("-50")),
        )
        self.assertFalse(Expense.objects.filter(description="Reconciliation adjustment").exists())
        self.assertEqual(self.row("cash")["last_check"]["difference"], "-50.00")

    def test_adjusting_writes_the_gap_into_the_books(self):
        self.earn("300", payment_method="cash")
        self.client.post(CHECKS, {"method": "cash", "counted": "250", "adjust": True}, format="json")

        self.assertEqual(Decimal(self.row("cash")["expected"]), Decimal("250"))
        self.assertTrue(Reconciliation.objects.get().adjusted)

        self.client.post(CHECKS, {"method": "bank", "counted": "80", "adjust": True}, format="json")
        self.assertEqual(Income.objects.get(description="Reconciliation adjustment").amount, Decimal("80"))

    def test_a_difference_on_the_opening_date_cannot_be_adjusted_by_an_entry(self):
        self.client.put(f"{OPENING}cash/", {"amount": "500", "as_of": str(self.today)}, format="json")
        response = self.client.post(CHECKS, {"method": "cash", "counted": "400", "adjust": True}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Reconciliation.objects.exists())

    def test_a_matching_count_needs_no_adjustment(self):
        self.earn("300", payment_method="cash")
        response = self.client.post(CHECKS, {"method": "cash", "counted": "300", "adjust": True}, format="json")

        self.assertEqual(Decimal(response.data["difference"]), 0)
        self.assertFalse(response.data["adjusted"])
        self.assertFalse(Expense.objects.filter(description="Reconciliation adjustment").exists())

    def test_dates_cannot_be_future_or_before_the_opening_balance(self):
        future = str(self.today + timedelta(days=1))
        self.assertEqual(self.client.put(f"{OPENING}cash/", {"amount": "1", "as_of": future}, format="json").status_code, 400)
        self.assertEqual(self.client.post(CHECKS, {"method": "cash", "counted": "1", "as_of": future}, format="json").status_code, 400)

        self.client.put(f"{OPENING}cash/", {"amount": "1", "as_of": str(self.today)}, format="json")
        earlier = str(self.today - timedelta(days=3))
        self.assertEqual(self.client.post(CHECKS, {"method": "cash", "counted": "1", "as_of": earlier}, format="json").status_code, 400)

    def test_entries_without_a_method_are_counted_for_the_owner_to_see(self):
        self.earn("10")
        data = self.client.get(BALANCES).json()

        self.assertEqual(data["without_method"], 1)

    def test_only_the_owner_and_only_their_own_business(self):
        self.client.post(CHECKS, {"method": "cash", "counted": "5"}, format="json")
        position = self.make_position(self.ho, "Books", finance=("view", "create", "update"))
        self.make_user("books", User.Role.STAFF, business=self.ho, position=position)
        self.authenticate("ACME-HO", "books")

        self.assertEqual(self.client.get(BALANCES).status_code, 403)
        self.assertEqual(self.client.post(CHECKS, {"method": "cash", "counted": "1"}, format="json").status_code, 403)

        self.authenticate("ACME-HO", "owner")
        self.assertEqual(len(self.client.get(CHECKS).json()), 1)

    def test_a_head_office_owner_can_look_but_not_change_a_branch(self):
        self.authenticate("ACME-BR", "owner")

        self.assertEqual(self.client.get(BALANCES).status_code, 200)
        self.assertEqual(self.client.post(CHECKS, {"method": "cash", "counted": "1"}, format="json").status_code, 403)


INCOME_URL = "/api/v1/finance/income/"
