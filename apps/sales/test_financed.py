from apps.sales.models import Followup
from apps.sales.test_bike_maintenance import BASE, FOLLOWUPS, BikeTestCase

PAYMENTS = "/api/v1/sales/payments/"


class FinancedSaleTests(BikeTestCase):

    def financed(self, **extra):
        terms = {
            "payment_mode": "financed",
            "customer": self.customer.pk,
            "financer_name": "Sunrise Finance",
            "financer_ref": "LN-77",
            "down_payment": "20000.00",
            **extra,
        }

        return self.bike_sale(**terms)

    def pay(self, sale, amount, **extra):
        return self.client.post(
            PAYMENTS,
            {"sale": sale["id"], "amount": str(amount), "method": "bank", **extra},
            format="json",
        )

    def confirmed(self, **extra):
        sale = self.financed(**extra).json()
        self.assertEqual(self.pay(sale, "20000").status_code, 201)
        done = self.client.post(f"{BASE}{sale['id']}/confirm/")
        self.assertEqual(done.status_code, 200, done.data)

        return done.json()

    def test_a_financed_sale_keeps_the_company_and_what_it_owes(self):
        response = self.financed()

        self.assertEqual(response.status_code, 201, response.data)
        data = response.json()
        self.assertEqual(data["payment_mode"], "financed")
        self.assertEqual(data["financer_name"], "Sunrise Finance")
        self.assertEqual(
            float(data["financer_receivable"]), float(data["amount_due"]) - 20000
        )
        self.assertGreater(float(data["financer_receivable"]), 0)

    def test_the_company_name_is_needed(self):
        response = self.financed(financer_name="")

        self.assertEqual(response.status_code, 400)
        self.assertIn("financer_name", response.json())

    def test_a_financed_sale_has_no_emi_details(self):
        response = self.financed(emi_months=6)

        self.assertEqual(response.status_code, 400)

    def test_it_needs_a_customer(self):
        response = self.financed(customer=None)

        self.assertEqual(response.status_code, 400)

    def test_the_company_is_for_financed_sales_only(self):
        response = self.bike_sale(financer_name="Sunrise Finance")

        self.assertEqual(response.status_code, 400)

    def test_the_down_payment_must_be_taken_before_confirming(self):
        sale = self.financed().json()
        response = self.client.post(f"{BASE}{sale['id']}/confirm/")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "down_payment_due")

    def test_confirming_opens_a_follow_up_for_the_company_payment(self):
        sale = self.confirmed(financer_due_date="2099-01-01")
        rows = [f for f in self.followups() if f["kind"] == "financer_due"]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["due_date"], "2099-01-01")
        self.assertEqual(rows[0]["amount"], sale["financer_receivable"])
        # No EMI schedule is kept by the dealer.
        self.assertFalse([f for f in self.followups() if f["kind"] == "emi_due"])

    def test_the_company_paying_closes_the_follow_up_and_settles_the_sale(self):
        sale = self.confirmed()
        paid = self.pay(sale, sale["financer_receivable"], payer="financer")

        self.assertEqual(paid.status_code, 201, paid.data)
        detail = self.client.get(f"{BASE}{sale['id']}/").json()
        self.assertEqual(detail["payment_status"], "paid")
        self.assertEqual(detail["financer_received"], sale["financer_receivable"])
        row = [f for f in self.followups() if f["kind"] == "financer_due"][0]
        self.assertEqual(row["status"], "done")

    def test_voiding_the_company_payment_opens_it_again(self):
        sale = self.confirmed()
        payment = self.pay(sale, sale["financer_receivable"], payer="financer").json()
        self.client.post(
            f"{PAYMENTS}{payment['id']}/void/", {"reason": "bounced"}, format="json"
        )
        row = [f for f in self.followups() if f["kind"] == "financer_due"][0]

        self.assertEqual(row["status"], "pending")

    def test_only_a_financed_confirmed_sale_takes_company_money(self):
        draft = self.financed().json()
        self.assertEqual(self.pay(draft, "1000", payer="financer").status_code, 400)

        plain = self.made_bike([self.unit('CH-002', 'EN-002')])
        self.assertEqual(self.pay(plain, "1000", payer="financer").status_code, 400)

    def test_the_customer_down_payment_cannot_be_voided_after_confirm(self):
        sale = self.confirmed()
        down = self.client.get(f"{BASE}{sale['id']}/").json()["payments"][0]
        response = self.client.post(
            f"{PAYMENTS}{down['id']}/void/", {"reason": "mistake"}, format="json"
        )

        self.assertEqual(response.status_code, 409)

    def test_the_company_balance_shows_in_the_receivables_report(self):
        sale = self.confirmed()
        report = self.client.get("/api/v1/reports/receivables/").json()

        self.assertEqual(
            float(report["financer_outstanding"]), float(sale["financer_receivable"])
        )


class FreeServiceFollowupTests(BikeTestCase):

    def confirmed(self, free=3, mode="full", **extra):
        sale = self.made_bike(free_services=free, **extra)
        self.client.post(
            PAYMENTS,
            {"sale": sale["id"], "amount": "100000", "method": "cash"},
            format="json",
        )
        done = self.client.post(f"{BASE}{sale['id']}/confirm/")
        self.assertEqual(done.status_code, 200, done.data)

        return done.json()

    def due(self):
        return sorted(
            (f for f in self.followups() if f["kind"] == "free_service_due"),
            key=lambda f: f["service_number"],
        )

    def test_every_free_service_gets_a_follow_up_after_confirming(self):
        sale = self.confirmed(free=3)
        rows = self.due()

        self.assertEqual([r["service_number"] for r in rows], [1, 2, 3])
        self.assertLess(rows[0]["due_date"], rows[1]["due_date"])
        self.assertEqual(rows[0]["sale"], sale["id"])

    def test_no_free_service_no_follow_up(self):
        self.confirmed(free=0)

        self.assertEqual(self.due(), [])

    def test_a_logged_service_closes_its_follow_up(self):
        sale = self.confirmed(free=2)
        self.client.post(
            f"{BASE}{sale['id']}/free-services/", {"odometer_km": 500}, format="json"
        )
        rows = self.due()

        self.assertEqual([r["status"] for r in rows], ["done", "pending"])

    def test_removing_the_record_opens_it_again(self):
        sale = self.confirmed(free=2)
        record = self.client.post(
            f"{BASE}{sale['id']}/free-services/", {"odometer_km": 500}, format="json"
        ).json()
        self.client.delete(f"{BASE}{sale['id']}/free-services/{record['id']}/")

        self.assertEqual([r["status"] for r in self.due()], ["pending", "pending"])

    def test_changing_the_count_adds_or_drops_follow_ups(self):
        sale = self.confirmed(free=2)
        self.client.post(
            f"{BASE}{sale['id']}/free-service-count/", {"free_services": 4}, format="json"
        )
        self.assertEqual(len(self.due()), 4)

        self.client.post(
            f"{BASE}{sale['id']}/free-service-count/", {"free_services": 1}, format="json"
        )
        self.assertEqual(len(self.due()), 1)

    def test_nothing_before_the_sale_is_confirmed(self):
        self.made_bike(free_services=3)

        self.assertFalse(Followup.objects.filter(kind="free_service_due").exists())
