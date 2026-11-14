"""The Accountant position: cash, collections, supplier and salary payment."""

from rest_framework.test import APITestCase

from apps.accounts import rbac
from apps.accounts.models import User
from apps.common.testing import TenantTestMixin

BALANCES = "/api/v1/finance/balances/"
RECONCILE = "/api/v1/finance/reconciliations/"
SALARIES = "/api/v1/payroll/salaries/"
PAYROLL_PAYMENTS = "/api/v1/payroll/payments/"
SALE_PAYMENTS = "/api/v1/sales/payments/"
PAYROLL_REPORT = "/api/v1/reports/payroll-summary/"


class AccountantTests(TenantTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.acc_pos = self.make_position(self.ho, "Accountant")
        rbac.write_position_codes(self.acc_pos, rbac.preset_codes("accountant"))
        self.acc = self.make_user(
            "acc", User.Role.STAFF, company=self.company, position=self.acc_pos
        )
        self.view_pos = self.make_position(
            self.ho, "Viewer", finance=("view",), sales=("view",)
        )
        self.viewer = self.make_user(
            "viewer", User.Role.STAFF, company=self.company, position=self.view_pos
        )

    def test_preset_carries_the_new_codes(self):
        codes = rbac.preset_codes("accountant")

        for code in (
            "finance.reconcile", "sales.collect", "purchases.pay",
            "staff.pay_view", "staff.pay",
        ):
            self.assertIn(code, codes)

        self.assertNotIn("sales.create", codes)
        self.assertNotIn("staff.update", codes)

    def test_accountant_reconciles_cash_and_bank(self):
        self.authenticate("ACME-HO", "acc")

        self.assertEqual(self.client.get(BALANCES).status_code, 200)
        self.assertEqual(self.client.get(RECONCILE).status_code, 200)

    def test_others_cannot_reconcile(self):
        self.authenticate("ACME-HO", "viewer")

        self.assertEqual(self.client.get(BALANCES).status_code, 403)
        self.assertEqual(self.client.get(RECONCILE).status_code, 403)

    def test_accountant_sees_and_records_pay_but_cannot_set_rates(self):
        self.authenticate("ACME-HO", "acc")

        self.assertEqual(self.client.get(SALARIES).status_code, 200)
        self.assertEqual(self.client.get(PAYROLL_REPORT).status_code, 200)
        self.assertEqual(self.client.get(PAYROLL_PAYMENTS).status_code, 200)
        # Allowed to try (bad body -> 400), never 403.
        self.assertEqual(
            self.client.post(PAYROLL_PAYMENTS, {}, format="json").status_code, 400
        )
        self.assertEqual(
            self.client.put(f"{SALARIES}{self.acc.pk}/", {}, format="json").status_code,
            403,
        )

    def test_others_cannot_see_or_pay_salaries(self):
        self.authenticate("ACME-HO", "viewer")

        self.assertEqual(self.client.get(SALARIES).status_code, 403)
        self.assertEqual(self.client.get(PAYROLL_REPORT).status_code, 403)
        self.assertEqual(
            self.client.post(PAYROLL_PAYMENTS, {}, format="json").status_code, 403
        )

    def test_pay_view_alone_cannot_record(self):
        pos = self.make_position(self.ho, "PayReader")
        rbac.write_position_codes(pos, {"staff.pay_view"})
        self.make_user("pr", User.Role.STAFF, company=self.company, position=pos)
        self.authenticate("ACME-HO", "pr")

        self.assertEqual(self.client.get(SALARIES).status_code, 200)
        self.assertEqual(
            self.client.post(PAYROLL_PAYMENTS, {}, format="json").status_code, 403
        )

    def test_collector_may_record_a_payment_a_viewer_may_not(self):
        self.authenticate("ACME-HO", "acc")
        self.assertEqual(
            self.client.post(SALE_PAYMENTS, {}, format="json").status_code, 400
        )

        self.authenticate("ACME-HO", "viewer")
        self.assertEqual(
            self.client.post(SALE_PAYMENTS, {}, format="json").status_code, 403
        )

    def test_the_accountant_still_cannot_create_sales(self):
        self.authenticate("ACME-HO", "acc")

        self.assertEqual(
            self.client.post("/api/v1/sales/sales/", {}, format="json").status_code,
            403,
        )
