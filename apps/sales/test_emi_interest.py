from datetime import timedelta

from django.utils import timezone

from apps.accounts.models import User
from apps.sales.models import EmiInstallment
from apps.sales.test_bike_maintenance import BASE, BikeTestCase, FOLLOWUPS, PAYMENTS

NOTES = "/api/v1/notifications/"


class EmiInterestTests(BikeTestCase):
    """The monthly amount (reducing-balance EMI) comes from an optional yearly interest."""

    def terms(self, **extra):
        return {
            "payment_mode": "emi", "down_payment": "20000.00",
            "emi_months": 10, "interest_rate": "12", **extra,
        }

    def test_interest_follows_the_reducing_balance_emi_formula(self):
        sale = self.made_bike(**self.terms())

        # 80000 left at 1% a month for 10 months: P r (1+r)^n / ((1+r)^n - 1).
        self.assertEqual(sale["emi_amount"], "8446.57")
        self.assertEqual(sale["finance_charge"], "4465.70")
        self.assertEqual(sale["amount_due"], "104465.70")
        self.assertEqual(sale["interest_rate"], "12.00")

    def test_no_interest_just_divides_what_is_left(self):
        sale = self.made_bike(**self.terms(interest_rate="0"))

        self.assertEqual(sale["emi_amount"], "8000.00")
        self.assertEqual(sale["finance_charge"], "0.00")

        # Leaving the rate out altogether means the same.
        terms = self.terms()
        terms.pop("interest_rate")
        other = self.made_bike(units=[self.unit(chassis="CH-2", engine="EN-2")], **terms)

        self.assertEqual(other["emi_amount"], "8000.00")

    def test_the_instalment_is_rounded_up_so_it_never_falls_short(self):
        sale = self.made_bike(**self.terms(down_payment="0", emi_months=3, interest_rate="0"))

        self.assertEqual(sale["emi_amount"], "33333.34")
        self.assertEqual(sale["finance_charge"], "0.02")

    def test_every_month_is_recorded_with_the_calculated_amount(self):
        sale = self.made_bike(**self.terms())
        self.pay(sale["id"], "20000.00")
        self.confirm(sale["id"])

        rows = self.detail(sale["id"])["installments"]

        self.assertEqual(len(rows), 10)
        self.assertEqual({r["amount"] for r in rows}, {"8446.57"})

    def test_changing_the_bike_price_works_the_amount_out_again(self):
        sale = self.made_bike(**self.terms())

        changed = self.client.patch(
            f"{BASE}{sale['id']}/",
            {"items": [{"bike_unit": sale["items"][0]["bike_unit"], "unit_price": "90000.00"}]},
            format="json",
        )

        self.assertEqual(changed.status_code, 200, changed.data)
        # 70000 left at 12% a year over 10 months.
        self.assertEqual(changed.json()["emi_amount"], "7390.75")

    def test_an_amount_given_without_a_rate_is_still_accepted(self):
        sale = self.made_bike(**self.emi_terms())

        self.assertEqual(sale["emi_amount"], "9000.00")
        self.assertIsNone(sale["interest_rate"])

    def test_interest_needs_an_emi_sale(self):
        response = self.bike_sale(interest_rate="10")

        self.assertEqual(response.status_code, 400)


class BikeAccessTests(BikeTestCase):
    """Only the owner and staff with the bike sales permission manage them."""

    def setUp(self):
        super().setUp()
        plain = self.make_position(self.ho, "Counter", sales=("view", "create", "update"))
        bike_view = self.make_position(
            self.ho, "Bike viewer", sales=("view",), bike_sales=("view",)
        )
        bike_mgr = self.make_position(
            self.ho, "Bike manager",
            sales=("view", "create", "update"), bike_sales=("view", "create", "update"),
        )
        for name, position in (("plain", plain), ("viewer", bike_view), ("manager", bike_mgr)):
            self.make_user(name, User.Role.STAFF, company=self.company, position=position)

        # A bike sale on EMI, confirmed, made by the owner.
        self.sale = self.made_bike(**self.emi_terms(first_emi_due=str(timezone.localdate() + timedelta(days=2))))
        self.pay(self.sale["id"], "20000.00")
        self.confirm(self.sale["id"])

    def as_user(self, name):
        self.authenticate("ACME-HO", name)

    def test_staff_without_the_permission_do_not_see_bike_sales_or_instalments(self):
        self.as_user("plain")

        listing = self.client.get(BASE).json()["results"]

        self.assertNotIn(self.sale["id"], [s["id"] for s in listing])
        self.assertEqual(self.client.get(f"{BASE}{self.sale['id']}/").status_code, 404)
        self.assertEqual(self.client.get(PAYMENTS).json()["results"], [])
        self.assertEqual(
            [f for f in self.client.get(FOLLOWUPS).json()["results"] if f["kind"] == "emi_due"], []
        )

    def test_staff_without_the_permission_cannot_sell_a_bike(self):
        self.as_user("plain")
        unit = self.unit_as_owner()

        response = self.bike_sale(units=[unit])

        self.assertEqual(response.status_code, 403)

    def unit_as_owner(self):
        self.authenticate("ACME-HO", "owner")
        unit = self.unit(chassis="CH-9", engine="EN-9")
        self.as_user("plain")

        return unit

    def test_a_viewer_sees_but_cannot_change_anything(self):
        self.as_user("viewer")

        self.assertEqual(self.client.get(f"{BASE}{self.sale['id']}/").status_code, 200)
        self.assertEqual(len(self.detail(self.sale["id"])["installments"]), 10)
        self.assertEqual(
            self.client.post(f"{BASE}{self.sale['id']}/cancel/", {"reason": "x"}, format="json").status_code,
            403,
        )
        self.assertEqual(self.pay(self.sale["id"], "100.00").status_code, 403)

    def test_a_bike_manager_records_instalment_payments(self):
        self.as_user("manager")

        self.assertEqual(self.pay(self.sale["id"], "9000.00").status_code, 201)

    def test_instalment_alerts_reach_the_owner_and_bike_managers_only(self):
        def emi_alerts(name):
            self.as_user(name)

            return [n for n in self.client.get(NOTES).json()["results"] if n["kind"].startswith("followup")]

        alerts = emi_alerts("manager")
        self.assertTrue(alerts)
        self.assertEqual(alerts[0]["link"], f"/dashboard/sales/detail?id={self.sale['id']}")
        self.assertEqual(emi_alerts("plain"), [])
        # A viewer may look at the schedule but is not the one chasing it.
        self.assertEqual(emi_alerts("viewer"), [])

        self.authenticate("ACME-HO", "owner")
        self.assertTrue([n for n in self.client.get(NOTES).json()["results"] if n["kind"].startswith("followup")])

    def test_the_owner_still_manages_bike_sales(self):
        self.authenticate("ACME-HO", "owner")

        self.assertEqual(self.pay(self.sale["id"], "9000.00").status_code, 201)
        self.assertEqual(EmiInstallment.objects.filter(sale_id=self.sale["id"]).count(), 10)
