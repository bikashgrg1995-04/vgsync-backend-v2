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
        ):
            self.assertIn(code, codes)

        self.assertNotIn("sales.create", codes)
        self.assertNotIn("staff.update", codes)
        self.assertNotIn("staff.pay_view", codes)
        self.assertNotIn("staff.pay", codes)

    def test_accountant_reconciles_cash_and_bank(self):
        self.authenticate("ACME-HO", "acc")

        self.assertEqual(self.client.get(BALANCES).status_code, 200)
        self.assertEqual(self.client.get(RECONCILE).status_code, 200)

    def test_others_cannot_reconcile(self):
        self.authenticate("ACME-HO", "viewer")

        self.assertEqual(self.client.get(BALANCES).status_code, 403)
        self.assertEqual(self.client.get(RECONCILE).status_code, 403)

    def test_payroll_is_owner_only(self):
        for who in ("acc", "viewer"):
            self.authenticate("ACME-HO", who)

            self.assertEqual(self.client.get(SALARIES).status_code, 403)
            self.assertEqual(self.client.get(PAYROLL_REPORT).status_code, 403)
            self.assertEqual(self.client.get(PAYROLL_PAYMENTS).status_code, 403)
            self.assertEqual(
                self.client.post(PAYROLL_PAYMENTS, {}, format="json").status_code, 403
            )
            self.assertEqual(
                self.client.put(f"{SALARIES}{self.acc.pk}/", {}, format="json").status_code,
                403,
            )

    def test_pay_codes_cannot_be_granted(self):
        self.assertIn("staff.pay_view", rbac.RESERVED)
        self.assertIn("staff.pay", rbac.RESERVED)

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
