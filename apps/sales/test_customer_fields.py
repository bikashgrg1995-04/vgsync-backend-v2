from apps.sales.models import Customer
from apps.sales.test_sales import CUSTOMERS, SaleTestCase


class CustomerTypeAndLimitTests(SaleTestCase):

    def add(self, **fields):
        return self.client.post(
            CUSTOMERS, {"name": "Fleet Co", **fields}, format="json"
        )

    def test_a_customer_is_an_individual_with_no_limit_by_default(self):
        data = self.add().json()

        self.assertEqual(data["customer_type"], "individual")
        self.assertEqual(data["credit_limit"], "0.00")
        self.assertFalse(data["over_credit_limit"])

    def test_type_and_limit_can_be_set_and_changed(self):
        created = self.add(customer_type="fleet", credit_limit="50000").json()
        self.assertEqual(created["customer_type"], "fleet")
        self.assertEqual(created["credit_limit"], "50000.00")

        changed = self.client.patch(
            f"{CUSTOMERS}{created['id']}/",
            {"customer_type": "business", "credit_limit": "75000.50"},
            format="json",
        ).json()
        self.assertEqual(changed["customer_type"], "business")
        self.assertEqual(changed["credit_limit"], "75000.50")

    def test_bad_type_and_negative_limit_are_refused(self):
        self.assertEqual(self.add(customer_type="vip").status_code, 400)

        negative = self.add(credit_limit="-1")
        self.assertEqual(negative.status_code, 400)
        self.assertIn("credit_limit", negative.json())

    def test_email_and_pan_are_optional(self):
        response = self.add(phone="9822222222")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.json()["email"], "")
        self.assertEqual(response.json()["pan_number"], "")

    def test_the_list_can_be_filtered_by_type(self):
        self.add(customer_type="fleet", name="Fleet A")
        self.add(customer_type="business", name="Biz A")

        fleet = self.client.get(CUSTOMERS, {"customer_type": "fleet"})
        self.assertEqual([c["name"] for c in fleet.data["results"]], ["Fleet A"])
        self.assertEqual(
            self.client.get(CUSTOMERS, {"customer_type": "individual"}).data["count"], 1
        )

    def test_owing_more_than_the_limit_is_a_warning_not_a_block(self):
        Customer.objects.filter(pk=self.customer.pk).update(credit_limit="1000")

        sale = self.confirmed()  # 10 x 150 = 1500, nothing paid
        figures = self.figures()

        self.assertEqual(figures["credit_to_receive"], "1500.00")
        self.assertTrue(figures["over_credit_limit"])

        self.pay(sale["id"], "600.00")
        self.assertFalse(self.figures()["over_credit_limit"])  # 900 <= 1000

    def test_no_limit_means_never_over(self):
        self.confirmed()

        self.assertFalse(self.figures()["over_credit_limit"])
        self.assertEqual(self.figures()["credit_limit"], "0.00")
