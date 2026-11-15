from apps.accounts import rbac
from apps.common.testing import User
from apps.sales.test_bike_maintenance import BASE, BikeTestCase

PAYMENTS = "/api/v1/sales/payments/"
FOLLOWUPS = "/api/v1/sales/followups/"
REPORT = "/api/v1/reports/salespeople/"


class SalespersonTests(BikeTestCase):

    def setUp(self):
        super().setUp()
        staff_pos = self.make_position(self.ho, "Sales Staff")
        rbac.write_position_codes(staff_pos, rbac.preset_codes("sales_staff"))
        mgr_pos = self.make_position(self.ho, "Sales Manager")
        rbac.write_position_codes(mgr_pos, rbac.preset_codes("sales_manager"))
        self.ram = self.make_user("ram", User.Role.STAFF, company=self.company, position=staff_pos)
        self.sita = self.make_user("sita", User.Role.STAFF, company=self.company, position=staff_pos)
        self.mgr = self.make_user("mgr", User.Role.STAFF, company=self.company, position=mgr_pos)

    def sale(self, **extra):
        return self.client.post(
            BASE,
            {
                "sale_type": "maintenance",
                "customer": self.customer.pk,
                "items": [{"description": "Service", "quantity": "1", "unit_price": "500"}],
                **extra,
            },
            format="json",
        )

    def test_the_seller_is_recorded(self):
        self.authenticate("ACME-HO", "ram")
        made = self.sale().json()

        self.assertEqual(made["salesperson"], self.ram.pk)
        self.assertEqual(made["salesperson_name"], self.ram.get_full_name() or "ram")

    def test_a_salesperson_sees_only_their_own_sales(self):
        self.authenticate("ACME-HO", "ram")
        mine = self.sale().json()
        self.authenticate("ACME-HO", "sita")
        theirs = self.sale().json()

        listing = self.client.get(BASE).json()["results"]
        self.assertEqual([s["id"] for s in listing], [theirs["id"]])
        self.assertEqual(self.client.get(f"{BASE}{mine['id']}/").status_code, 404)

    def test_their_payments_and_followups_follow(self):
        self.authenticate("ACME-HO", "ram")
        mine = self.sale().json()
        self.client.post(
            PAYMENTS,
            {"sale": mine["id"], "amount": "100", "method": "cash"},
            format="json",
        )
        self.authenticate("ACME-HO", "sita")

        self.assertEqual(self.client.get(PAYMENTS).json()["results"], [])
        self.assertEqual(self.client.get(FOLLOWUPS).json()["results"], [])

    def test_a_manager_sees_all_and_can_credit_someone(self):
        self.authenticate("ACME-HO", "ram")
        self.sale()
        self.authenticate("ACME-HO", "mgr")
        made = self.sale(salesperson=self.sita.pk).json()

        self.assertEqual(made["salesperson"], self.sita.pk)
        self.assertEqual(len(self.client.get(BASE).json()["results"]), 2)
        filtered = self.client.get(BASE, {"salesperson": self.sita.pk}).json()["results"]
        self.assertEqual([s["id"] for s in filtered], [made["id"]])

    def test_a_salesperson_cannot_credit_another_person(self):
        self.authenticate("ACME-HO", "ram")

        self.assertEqual(self.sale(salesperson=self.sita.pk).status_code, 403)

    def test_the_owner_sees_everything(self):
        self.authenticate("ACME-HO", "ram")
        self.sale()
        self.authenticate("ACME-HO", "owner")

        self.assertEqual(len(self.client.get(BASE).json()["results"]), 1)

    def test_the_report_totals_per_salesperson(self):
        self.authenticate("ACME-HO", "ram")
        made = self.sale().json()
        self.client.post(f"{BASE}{made['id']}/confirm/", {}, format="json")
        self.authenticate("ACME-HO", "mgr")
        rows = self.client.get(REPORT)

        self.assertEqual(rows.status_code, 200, rows.content)
        mine = [r for r in rows.json()["rows"] if r["salesperson"] == self.ram.pk]

        self.assertEqual(len(mine), 1, rows.json())
        self.assertEqual(mine[0]["sales"], 1)
        self.assertEqual(mine[0]["total"], "500.00")
        self.assertEqual(mine[0]["due"], "500.00")
