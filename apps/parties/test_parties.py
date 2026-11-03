from datetime import timedelta

from django.utils import timezone

from apps.accounts.models import User
from apps.purchases.models import Supplier
from apps.purchases.test_purchases import SUPPLIERS as SUPPLIERS_URL
from apps.purchases.test_returns import ReturnTestCase as PurchaseReturnCase
from apps.sales.test_sale_returns import ReturnTestCase as SaleReturnCase
from apps.sales.test_sales import BASE, PAYMENTS
from apps.sales.test_sales import CUSTOMERS as CUSTOMERS_URL

OLD_CUSTOMERS = "/api/v1/sales/customers/"
OLD_SUPPLIERS = "/api/v1/purchases/suppliers/"


def days_ago(days):
    return str(timezone.localdate() - timedelta(days=days))


class CustomerStatementTests(SaleReturnCase):

    def statement(self, customer=None, **params):
        url = f"{CUSTOMERS_URL}{(customer or self.customer).pk}/statement/"
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200, response.content)

        return response.json()

    def rows(self, **params):
        return self.statement(**params)["results"]

    def trail(self, **params):
        return [(r["type"], r["debit"], r["credit"], r["balance"]) for r in self.rows(**params)]

    def story(self):
        """1500 sale, 1000 paid, 4 oil back (600), then the 100 refunded."""
        sale = self.confirmed()
        self.pay(sale["id"], "1000.00")
        self.returned(sale, "4")
        self.refund("100.00")

        return sale

    def test_the_statement_lists_everything_with_a_running_balance(self):
        self.story()

        data = self.statement()

        self.assertEqual(
            self.trail(),
            [
                ("sale", "1500.00", "0.00", "1500.00"),
                ("payment", "0.00", "1000.00", "500.00"),
                ("return", "0.00", "600.00", "-100.00"),
                ("refund", "100.00", "0.00", "0.00"),
            ],
        )
        self.assertEqual(data["party"]["name"], "Ram Auto")
        self.assertEqual(data["opening_balance"], "0.00")
        self.assertEqual(data["total_debit"], "1600.00")
        self.assertEqual(data["total_credit"], "1600.00")
        self.assertEqual(data["closing_balance"], "0.00")
        self.assertEqual(data["count"], 4)

    def test_rows_carry_their_reference_and_document(self):
        sale = self.story()

        rows = self.rows()

        self.assertEqual(rows[0]["reference"], "INV-000001")
        self.assertEqual(rows[0]["document"], "sale")
        self.assertEqual(rows[0]["document_id"], sale["id"])
        self.assertEqual(rows[2]["reference"], "SRT-000001")
        self.assertIn("SAL-000001", rows[1]["description"])

    def test_the_balance_matches_the_profile_figures(self):
        sale = self.confirmed()
        self.pay(sale["id"], "400.00")
        self.returned(sale, "2")

        profile = self.figures()
        data = self.statement()

        owed = dec(profile["credit_to_receive"]) - dec(profile["credit_to_pay"])
        self.assertEqual(dec(data["closing_balance"]), owed)

    def test_credit_owed_back_shows_as_a_negative_balance(self):
        sale = self.paid_sale()
        self.returned(sale, "4")

        data = self.statement()

        self.assertEqual(data["closing_balance"], "-600.00")
        self.assertEqual(self.figures()["credit_to_pay"], "600.00")

    def test_setting_credit_against_a_sale_does_not_change_the_balance(self):
        paid = self.paid_sale()
        self.returned(paid, "4")
        other = self.confirmed(items=[self.line(quantity="2", price="100.00")])
        before = self.statement()["closing_balance"]

        self.apply(other["id"], "200.00")
        data = self.statement()

        self.assertEqual(before, "-400.00")
        self.assertEqual(data["closing_balance"], "-400.00")
        row = [r for r in data["results"] if r["type"] == "adjustment"][0]
        self.assertEqual((row["debit"], row["credit"]), ("0.00", "0.00"))
        self.assertIn("200.00", row["description"])
        self.assertEqual(self.figures()["credit_to_pay"], "400.00")

    def test_replacement_goods_turn_credit_into_an_obligation_of_goods(self):
        sale = self.paid_sale()
        self.returned(sale, "4")

        self.replace("160.00")
        data = self.statement()

        row = [r for r in data["results"] if r["type"] == "replacement"][0]
        self.assertEqual(row["debit"], "160.00")
        self.assertIn("to be delivered", row["description"])
        self.assertEqual(data["closing_balance"], "-440.00")

    def test_an_advance_on_a_draft_is_a_credit(self):
        draft = self.made()
        self.pay(draft["id"], "300.00")

        data = self.statement()

        self.assertEqual(self.trail(), [("payment", "0.00", "300.00", "-300.00")])
        self.assertEqual(data["closing_balance"], "-300.00")

    def test_voided_and_cancelled_records_are_left_out(self):
        sale = self.confirmed()
        payment = self.pay(sale["id"], "100.00").json()
        self.client.post(f"{PAYMENTS}{payment['id']}/void/", {"reason": "x"}, format="json")
        cancelled = self.made()
        self.client.post(f"{BASE}{cancelled['id']}/cancel/", {"reason": "x"}, format="json")
        self.made()

        self.assertEqual([r["type"] for r in self.rows()], ["sale"])

    def test_an_emi_sale_is_charged_with_its_finance_charge(self):
        sale = self.made_bike(**self.emi_terms())
        self.pay(sale["id"], "20000.00")
        self.confirm(sale["id"])

        data = self.statement()

        self.assertEqual(data["results"][0]["debit"], "110000.00")
        self.assertEqual(data["closing_balance"], "90000.00")
        self.assertEqual(self.figures()["credit_to_receive"], "90000.00")

    def test_a_date_range_carries_the_opening_balance(self):
        old = self.made(sale_date=days_ago(20))
        self.confirm(old["id"])
        self.pay(old["id"], "500.00", paid_on=days_ago(15))
        recent = self.confirmed(sale_date=days_ago(2))
        self.pay(recent["id"], "1500.00", paid_on=days_ago(1))

        data = self.statement(date_from=days_ago(5))

        self.assertEqual(data["opening_balance"], "1000.00")
        self.assertEqual(
            [(r["type"], r["balance"]) for r in data["results"]],
            [("sale", "2500.00"), ("payment", "1000.00")],
        )
        self.assertEqual(data["closing_balance"], "1000.00")
        self.assertEqual(data["total_debit"], "1500.00")
        self.assertEqual(data["total_credit"], "1500.00")

    def test_date_to_stops_the_statement_and_its_closing_balance(self):
        old = self.confirmed(sale_date=days_ago(20))
        self.pay(old["id"], "500.00", paid_on=days_ago(2))

        data = self.statement(date_to=days_ago(10))

        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["closing_balance"], "1500.00")

    def test_an_empty_period_still_shows_the_balance(self):
        self.confirmed(sale_date=days_ago(20))

        data = self.statement(date_from=days_ago(5))

        self.assertEqual(data["results"], [])
        self.assertEqual(data["opening_balance"], "1500.00")
        self.assertEqual(data["closing_balance"], "1500.00")

    def test_filtering_by_type_keeps_the_true_balance(self):
        self.story()

        data = self.statement(type="payment")

        self.assertEqual(self.trail(type="payment"), [("payment", "0.00", "1000.00", "500.00")])
        self.assertEqual(data["total_credit"], "1000.00")
        self.assertEqual(len(self.rows(type="payment,refund")), 2)
        self.assertEqual(len(self.rows(type=["sale", "return"])), 2)

    def test_bad_filters_are_refused(self):
        url = f"{CUSTOMERS_URL}{self.customer.pk}/statement/"

        for params in (
            {"date_from": "yesterday"},
            {"date_to": "2026-13-45"},
            {"date_from": "2026-02-01", "date_to": "2026-01-01"},
            {"type": "purchase"},
        ):
            self.assertEqual(self.client.get(url, params).status_code, 400, params)

    def test_the_statement_is_paged(self):
        for _ in range(3):
            self.confirmed()

        data = self.client.get(
            f"{CUSTOMERS_URL}{self.customer.pk}/statement/", {"page_size": 2}
        ).json()

        self.assertEqual(data["count"], 3)
        self.assertEqual(len(data["results"]), 2)
        self.assertIsNotNone(data["next"])
        # The balance covers all of it, not only the page.
        self.assertEqual(data["closing_balance"], "4500.00")

    def test_a_customer_with_no_history_has_an_empty_statement(self):
        data = self.statement(customer=self.make_customer())

        self.assertEqual(data["results"], [])
        self.assertEqual(data["closing_balance"], "0.00")

    def make_customer(self):
        from apps.sales.models import Customer

        return Customer.objects.create(business=self.ho, name="Quiet Buyer")

    def test_other_customers_do_not_leak_in(self):
        self.confirmed()
        quiet = self.make_customer()

        self.assertEqual(self.rows(customer=quiet), [])


class CustomerSectionTests(SaleReturnCase):

    def test_customers_live_in_their_own_section(self):
        listed = self.client.get(CUSTOMERS_URL)
        old = self.client.get(OLD_CUSTOMERS)

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data["count"], 1)
        self.assertEqual(old.status_code, 404)

    def test_the_profile_shows_both_directions(self):
        sale = self.paid_sale()
        self.returned(sale, "4")
        self.replace("160.00")

        profile = self.figures()

        self.assertEqual(profile["credit_to_receive"], "0.00")
        self.assertEqual(profile["credit_to_pay"], "440.00")
        self.assertEqual(profile["stock_to_give"], "2.000")

    def test_access_follows_the_sales_permission(self):
        position = self.make_position(self.ho, "Viewer", sales=("view",))
        self.make_user("viewer", User.Role.STAFF, company=self.company, position=position)
        stock = self.make_position(self.ho, "Stock", inventory=("view",))
        self.make_user("keeper", User.Role.STAFF, company=self.company, position=stock)
        url = f"{CUSTOMERS_URL}{self.customer.pk}/statement/"

        self.authenticate("ACME-HO", "viewer")
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(
            self.client.post(CUSTOMERS_URL, {"name": "N"}, format="json").status_code, 403
        )

        self.authenticate("ACME-HO", "keeper")
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_another_company_cannot_read_a_statement(self):
        self.authenticate("OTHER-HO", "owner")

        response = self.client.get(f"{CUSTOMERS_URL}{self.customer.pk}/statement/")

        self.assertEqual(response.status_code, 404)

    def test_a_branch_owner_sees_only_the_branch(self):
        self.authenticate("ACME-BR", "owner")

        self.assertEqual(self.client.get(CUSTOMERS_URL).data["count"], 0)
        self.assertEqual(
            self.client.get(f"{CUSTOMERS_URL}{self.customer.pk}/statement/").status_code, 404
        )

    def test_unauthenticated_requests_are_rejected(self):
        self.client.credentials()

        self.assertEqual(self.client.get(CUSTOMERS_URL).status_code, 401)
        self.assertEqual(
            self.client.get(f"{CUSTOMERS_URL}{self.customer.pk}/statement/").status_code, 401
        )


def dec(value):
    from decimal import Decimal

    return Decimal(str(value))


class SupplierStatementTests(PurchaseReturnCase):

    def statement(self, supplier=None, **params):
        url = f"{SUPPLIERS_URL}{(supplier or self.supplier).pk}/statement/"
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200, response.content)

        return response.json()

    def trail(self, **params):
        return [
            (r["type"], r["debit"], r["credit"], r["balance"])
            for r in self.statement(**params)["results"]
        ]

    def replace(self, amount="400.00", items=None, **extra):
        payload = {
            "supplier": self.supplier.pk,
            "kind": "replacement",
            "amount": amount,
            "items": items if items is not None else [
                {"product": self.oil.pk, "quantity": "4"}
            ],
            **extra,
        }

        return self.client.post("/api/v1/purchases/credits/", payload, format="json")

    def story(self):
        """1000 bought and received, 400 paid, 4 sent back (400), 100 refunded."""
        purchase = self.received()
        self.pay(purchase["id"], "400.00")
        self.returned(purchase, "4")
        self.refund("100.00")

        return purchase

    def test_the_statement_lists_everything_with_a_running_balance(self):
        self.story()

        data = self.statement()

        self.assertEqual(
            self.trail(),
            [
                ("purchase", "1000.00", "0.00", "1000.00"),
                ("payment", "0.00", "400.00", "600.00"),
                ("return", "0.00", "400.00", "200.00"),
                ("refund", "100.00", "0.00", "300.00"),
            ],
        )
        self.assertEqual(data["closing_balance"], "300.00")
        self.assertEqual(data["party"]["name"], "Himal Traders")

    def test_the_balance_matches_the_profile_figures(self):
        self.story()

        profile = self.client.get(f"{SUPPLIERS_URL}{self.supplier.pk}/").json()
        data = self.statement()

        owed = dec(profile["credit_to_pay"]) - dec(profile["credit_to_receive"])
        self.assertEqual(dec(data["closing_balance"]), owed)

    def test_an_order_counts_from_the_day_it_is_made(self):
        self.made()

        data = self.statement()

        self.assertEqual(data["closing_balance"], "1000.00")
        self.assertIn("ordered", data["results"][0]["description"])

    def test_cancelled_orders_and_void_payments_are_left_out(self):
        cancelled = self.made()
        self.client.post(
            f"/api/v1/purchases/{cancelled['id']}/cancel/", {"reason": "x"}, format="json"
        )
        purchase = self.received()
        payment = self.pay(purchase["id"], "100.00").json()
        self.client.post(
            f"/api/v1/purchases/payments/{payment['id']}/void/", {"reason": "x"}, format="json"
        )

        self.assertEqual([r[0] for r in self.trail()], ["purchase"])

    def test_credit_applied_to_a_purchase_does_not_change_the_balance(self):
        old = self.received()
        self.returned(old, "4")
        new = self.received(items=[self.line(quantity="3", price="100.00")])
        before = self.statement()["closing_balance"]

        self.apply(new["id"], "300.00")

        self.assertEqual(self.statement()["closing_balance"], before)
        self.assertEqual(before, "900.00")

    def test_a_replacement_is_a_debit_until_the_goods_arrive(self):
        purchase = self.received()
        self.returned(purchase, "4")

        self.replace("400.00")
        data = self.statement()

        row = [r for r in data["results"] if r["type"] == "replacement"][0]
        self.assertEqual(row["debit"], "400.00")
        self.assertIn("to be received", row["description"])

    def test_a_date_range_and_type_filter(self):
        old = self.made(purchase_date=days_ago(30))
        self.receive(old["id"])
        self.pay(old["id"], "300.00", paid_on=days_ago(1))

        data = self.statement(date_from=days_ago(10))
        only = self.statement(type="purchase")

        self.assertEqual(data["opening_balance"], "1000.00")
        self.assertEqual([r["type"] for r in data["results"]], ["payment"])
        self.assertEqual(data["closing_balance"], "700.00")
        self.assertEqual(only["results"][0]["balance"], "1000.00")
        self.assertEqual(only["closing_balance"], "700.00")

    def test_bad_filters_are_refused(self):
        url = f"{SUPPLIERS_URL}{self.supplier.pk}/statement/"

        for params in ({"date_from": "x"}, {"type": "sale"}):
            self.assertEqual(self.client.get(url, params).status_code, 400, params)

    def test_suppliers_live_in_their_own_section(self):
        self.assertEqual(self.client.get(SUPPLIERS_URL).status_code, 200)
        self.assertEqual(self.client.get(OLD_SUPPLIERS).status_code, 404)

    def test_access_follows_the_purchases_permission(self):
        viewer = self.make_position(self.ho, "Viewer", purchases=("view",))
        self.make_user("viewer", User.Role.STAFF, company=self.company, position=viewer)
        sales = self.make_position(self.ho, "Sales", sales=("view",))
        self.make_user("seller", User.Role.STAFF, company=self.company, position=sales)
        url = f"{SUPPLIERS_URL}{self.supplier.pk}/statement/"

        self.authenticate("ACME-HO", "viewer")
        self.assertEqual(self.client.get(url).status_code, 200)

        self.authenticate("ACME-HO", "seller")
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_another_company_cannot_read_a_statement(self):
        self.authenticate("OTHER-HO", "owner")

        response = self.client.get(f"{SUPPLIERS_URL}{self.supplier.pk}/statement/")

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Supplier.objects.filter(pk=self.supplier.pk).exists())
