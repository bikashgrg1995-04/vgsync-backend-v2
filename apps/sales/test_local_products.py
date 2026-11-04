from decimal import Decimal

from apps.inventory.models import LocalProduct, StockMovement
from apps.sales.models import Sale
from apps.sales.test_sale_returns import ReturnTestCase
from apps.sales.test_sales import BASE, dec

LOCAL = "/api/v1/sales/local-products/"


class LocalProductTestCase(ReturnTestCase):
    def setUp(self):
        super().setUp()

        self.wash = LocalProduct.objects.create(
            business=self.ho, name="Bike Wash", unit="job",
            sale_price=Decimal("300.00"),
        )
        self.helmet = LocalProduct.objects.create(
            business=self.ho, name="Helmet", brand="Studds",
            purchase_price=Decimal("900.00"), sale_price=Decimal("1500.00"),
        )
        self.free = LocalProduct.objects.create(business=self.ho, name="Unpriced")
        self.other_local = LocalProduct.objects.create(
            business=self.other_ho, name="Other Wash", sale_price=Decimal("50.00")
        )

    def local_line(self, local=None, quantity="1", price=None):
        data = {"local_product": (local or self.wash).pk, "quantity": quantity}

        if price is not None:
            data["unit_price"] = price

        return data

    def moves(self):
        return StockMovement.objects.filter(
            movement_type=StockMovement.MovementType.SALE_ISSUE
        ).count()


class LocalProductApiTests(LocalProductTestCase):
    def test_a_local_product_is_added(self):
        response = self.client.post(
            LOCAL,
            {"name": "Tyre Fitting", "unit": "job", "sale_price": "200.00"},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["sale_price"], "200.00")
        self.assertTrue(response.data["is_active"])
        self.assertEqual(
            LocalProduct.objects.get(pk=response.data["id"]).business, self.ho
        )

    def test_name_and_price_rules(self):
        for payload in (
            {"name": "bike wash"},          # same name, other case
            {"name": " "},
            {"name": "Ok", "sale_price": "-1"},
            {"name": "Ok", "purchase_price": "-1"},
            {"name": "Ok", "sale_price": "abc"},
        ):
            self.assertEqual(
                self.client.post(LOCAL, payload, format="json").status_code, 400, payload
            )

    def test_the_same_name_is_fine_in_another_business(self):
        self.authenticate("OTHER-HO", "owner")

        response = self.client.post(LOCAL, {"name": "Bike Wash"}, format="json")

        self.assertEqual(response.status_code, 201)

    def test_only_this_business_is_listed(self):
        listed = self.client.get(LOCAL).json()

        self.assertEqual(listed["count"], 3)
        names = [row["name"] for row in listed["results"]]
        self.assertNotIn("Other Wash", names)
        self.assertEqual(
            self.client.get(f"{LOCAL}{self.other_local.pk}/").status_code, 404
        )

    def test_edit_filter_and_search(self):
        edited = self.client.patch(
            f"{LOCAL}{self.wash.pk}/", {"sale_price": "350.00"}, format="json"
        )

        self.assertEqual(edited.data["sale_price"], "350.00")
        self.assertEqual(self.client.get(LOCAL, {"search": "studds"}).json()["count"], 1)

    def test_delete_only_deactivates(self):
        response = self.client.delete(f"{LOCAL}{self.wash.pk}/")

        self.assertEqual(response.status_code, 200)
        self.wash.refresh_from_db()
        self.assertFalse(self.wash.is_active)
        self.assertEqual(self.client.get(LOCAL, {"is_active": "true"}).json()["count"], 2)

    def test_it_needs_a_login(self):
        self.client.credentials()

        self.assertEqual(self.client.get(LOCAL).status_code, 401)


class SellingLocalProductsTests(LocalProductTestCase):
    def test_price_defaults_and_the_line_is_named_after_the_product(self):
        sale = self.made(items=[self.local_line(self.wash, "2")])

        line = sale["items"][0]

        self.assertEqual(line["unit_price"], "300.00")
        self.assertEqual(line["line_total"], "600.00")
        self.assertEqual(line["description"], "Bike Wash")
        self.assertEqual(line["product_name"], "Bike Wash")
        self.assertEqual(line["local_product"], self.wash.pk)
        self.assertIsNone(line["product"])
        self.assertEqual(sale["net_total"], "600.00")

    def test_the_price_can_be_changed_on_the_line(self):
        sale = self.made(items=[self.local_line(self.wash, "1", price="250.00")])

        self.assertEqual(sale["items"][0]["unit_price"], "250.00")

    def test_a_local_product_without_a_price_needs_one(self):
        refused = self.create(items=[self.local_line(self.free)])
        fine = self.create(items=[self.local_line(self.free, price="40.00")])

        self.assertEqual(refused.status_code, 400)
        self.assertEqual(fine.status_code, 201)

    def test_stock_and_local_lines_mix_in_one_sale(self):
        sale = self.confirmed(items=[self.line(self.oil, "2"), self.local_line(self.helmet)])

        # 2 x 150 + 1 x 1500
        self.assertEqual(sale["net_total"], "1800.00")
        self.assertEqual(self.stock(self.oil), dec("48"))

    def test_confirming_takes_nothing_from_stock_for_local_lines(self):
        before = self.moves()
        sale = self.confirmed(items=[self.local_line(self.wash, "3")])

        self.assertEqual(sale["status"], "confirmed")
        self.assertEqual(self.moves(), before)

    def test_a_sale_of_local_products_alone_is_fine_with_no_stock_at_all(self):
        sale = self.confirmed(items=[self.local_line(self.helmet, "5")])

        self.assertEqual(sale["net_total"], "7500.00")

    def test_payment_and_income_work_as_for_any_sale(self):
        sale = self.confirmed(items=[self.local_line(self.helmet)])
        self.assertEqual(self.pay(sale["id"], "1500.00").status_code, 201)

        income = self.client.get("/api/v1/finance/income/").json()

        self.assertEqual(income["count"], 1)
        self.assertEqual(income["results"][0]["category"], "stock_sale")
        self.assertEqual(self.detail(sale["id"])["payment_status"], "paid")

    def test_a_maintenance_sale_takes_parts_local_products_and_service(self):
        sale = self.made(
            sale_type="maintenance",
            items=[
                self.line(self.oil, "1"),
                self.local_line(self.wash),
                {"description": "Labour", "quantity": "1", "unit_price": "500.00"},
            ],
        )

        self.assertEqual(sale["net_total"], "950.00")
        self.assertEqual(
            self.confirm(sale["id"]).status_code, 200
        )
        self.assertEqual(self.stock(self.oil), dec("49"))

    def test_a_bike_sale_cannot_have_local_products(self):
        unit = self.unit()

        response = self.client.post(
            BASE,
            {
                "sale_type": "bike",
                "customer": self.customer.pk,
                "items": [{"bike_unit": unit["id"], "local_product": self.wash.pk}],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_mixed_lines_are_refused(self):
        both = {"product": self.oil.pk, "local_product": self.wash.pk, "quantity": "1"}
        described = {**self.local_line(), "description": "Custom text"}

        self.assertEqual(self.create(items=[both]).status_code, 400)
        self.assertEqual(self.create(items=[described]).status_code, 400)

    def test_a_line_needs_a_quantity(self):
        response = self.create(items=[{"local_product": self.wash.pk}])

        self.assertEqual(response.status_code, 400)

    def test_inactive_and_foreign_local_products_cannot_be_sold(self):
        self.helmet.is_active = False
        self.helmet.save()

        inactive = self.create(items=[self.local_line(self.helmet)])
        foreign = self.create(items=[self.local_line(self.other_local)])

        self.assertEqual(inactive.status_code, 400)
        self.assertEqual(foreign.status_code, 400)

    def test_a_product_deactivated_after_the_draft_still_sells(self):
        sale = self.made(items=[self.local_line(self.wash)])
        self.wash.is_active = False
        self.wash.save()

        self.assertEqual(self.confirm(sale["id"]).status_code, 200)

    def test_a_draft_can_swap_a_stock_line_for_a_local_one(self):
        sale = self.made()

        response = self.client.patch(
            f"{BASE}{sale['id']}/",
            {"items": [self.local_line(self.wash, "2")]},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["net_total"], "600.00")

    def test_a_local_line_cannot_be_returned(self):
        sale = self.confirmed(
            items=[self.line(self.oil, "2"), self.local_line(self.wash)]
        )
        local_line = sale["items"][1]
        self.assertEqual(local_line["returnable_quantity"], "0.000")

        response = self.client.post(
            "/api/v1/sales/returns/",
            {
                "sale": sale["id"], "reason": "x",
                "items": [{"sale_item": local_line["id"], "quantity": "1"}],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("local product", str(response.json()))
        # The stock line next to it still returns normally.
        self.assertEqual(self.send_back(sale, "1").status_code, 201)

    def test_the_database_refuses_a_line_with_both_kinds_of_product(self):
        from django.db import IntegrityError, transaction

        from apps.sales.models import SaleItem

        sale = Sale.objects.get(pk=self.made()["id"])

        with self.assertRaises(IntegrityError), transaction.atomic():
            SaleItem.objects.create(
                sale=sale, product=self.oil, local_product=self.wash,
                quantity=1, unit_price=10, line_total=10,
            )

    def test_the_customer_statement_counts_the_sale(self):
        self.confirmed(items=[self.local_line(self.helmet)])

        figures = self.client.get(f"/api/v1/customers/{self.customer.pk}/").json()

        self.assertEqual(figures["credit_to_receive"], "1500.00")
