from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from apps.inventory.models import Product, StockMovement
from apps.inventory.services.stock import create_stock_movement
from apps.purchases.models import Purchase
from apps.purchases.test_purchases import PurchaseTestCase
from apps.sales.models import Sale
from apps.sales.test_sale_returns import ReturnTestCase

PROFIT = "/api/v1/reports/profit-loss/"
RECEIVABLES = "/api/v1/reports/receivables/"
PAYABLES = "/api/v1/reports/payables/"
STOCK = "/api/v1/reports/stock-value/"
EXPENSES = "/api/v1/finance/expenses/"
INCOME = "/api/v1/finance/income/"


def days_ago(days):
    return timezone.localdate() - timedelta(days=days)


class ProfitLossTests(ReturnTestCase):
    def spend(self, amount, **extra):
        return self.client.post(
            EXPENSES, {"category": "rent", "amount": amount, **extra}, format="json"
        )

    def test_profit_is_income_less_expenses(self):
        self.paid_sale()
        self.spend("200.00")

        report = self.client.get(PROFIT).json()

        self.assertEqual(report["income"]["total"], "1500.00")
        self.assertEqual(report["expenses"]["total"], "200.00")
        self.assertEqual(report["profit"], "1300.00")
        self.assertEqual(report["income"]["by_category"][0]["category"], "stock_sale")

    def test_other_income_counts(self):
        self.client.post(INCOME, {"category": "other", "amount": "75"}, format="json")

        self.assertEqual(self.client.get(PROFIT).json()["profit"], "75.00")

    def test_a_refund_to_a_customer_lowers_income(self):
        sale = self.paid_sale()
        self.returned(sale, "4")  # worth 600, credit to pay
        self.assertEqual(self.refund("600.00").status_code, 201)

        report = self.client.get(PROFIT).json()

        self.assertEqual(report["income"]["total"], "1500.00")
        self.assertEqual(report["income"]["sale_refunds"], "600.00")
        self.assertEqual(report["income"]["net"], "900.00")
        self.assertEqual(report["profit"], "900.00")

    def test_a_return_without_a_refund_moves_no_cash(self):
        sale = self.paid_sale()
        self.returned(sale, "4")

        self.assertEqual(self.client.get(PROFIT).json()["profit"], "1500.00")

    def test_void_records_are_left_out(self):
        sale = self.paid_sale()
        gone = self.spend("999.00").json()
        self.client.post(
            f"{EXPENSES}{gone['id']}/void/", {"reason": "x"}, format="json"
        )

        report = self.client.get(PROFIT).json()

        self.assertEqual(report["expenses"]["total"], "0.00")
        self.assertEqual(sale["payment_status"], "paid")

    def test_the_date_range_and_months_split(self):
        self.spend("100.00", expense_date=str(days_ago(70)))
        self.spend("40.00")

        everything = self.client.get(PROFIT).json()
        recent = self.client.get(PROFIT, {"date_from": str(days_ago(5))}).json()

        self.assertEqual(everything["expenses"]["total"], "140.00")
        self.assertEqual(recent["expenses"]["total"], "40.00")
        self.assertGreaterEqual(len(everything["by_month"]), 2)
        months = {row["month"]: row for row in everything["by_month"]}
        self.assertEqual(
            sum(Decimal(row["expenses"]) for row in months.values()), Decimal("140.00")
        )

    def test_bad_or_reversed_dates_are_refused(self):
        for params in (
            {"date_from": "2026-13-45"},
            {"date_to": "nope"},
            {"date_from": "2026-05-02", "date_to": "2026-05-01"},
        ):
            self.assertEqual(self.client.get(PROFIT, params).status_code, 400, params)

    def test_figures_are_for_the_active_business_only(self):
        self.paid_sale()
        self.authenticate("OTHER-HO", "owner")

        self.assertEqual(self.client.get(PROFIT).json()["profit"], "0.00")


class ReceivablesTests(ReturnTestCase):
    def age(self, sale, days):
        Sale.objects.filter(pk=sale["id"]).update(sale_date=days_ago(days))

    def test_unpaid_balance_is_listed_by_customer(self):
        sale = self.confirmed()
        self.pay(sale["id"], "500.00")

        report = self.client.get(RECEIVABLES).json()

        self.assertEqual(report["total"], "1000.00")
        self.assertEqual(report["buckets"]["current"], "1000.00")
        self.assertEqual(report["parties"][0]["name"], "Ram Auto")
        self.assertEqual(report["parties"][0]["total"], "1000.00")

    def test_it_matches_the_customer_figure(self):
        sale = self.confirmed()
        self.pay(sale["id"], "500.00")

        report = self.client.get(RECEIVABLES).json()

        self.assertEqual(report["total"], self.figures()["credit_to_receive"])

    def test_paid_drafts_and_cancelled_sales_are_left_out(self):
        paid = self.confirmed()
        self.pay(paid["id"], paid["amount_due"])
        self.made()  # a draft owes nothing yet

        self.assertEqual(self.client.get(RECEIVABLES).json()["total"], "0.00")

    def test_old_balances_fall_into_older_buckets(self):
        recent = self.confirmed()
        month = self.confirmed()
        quarter = self.confirmed()
        ancient = self.confirmed()
        self.age(recent, 5)
        self.age(month, 45)
        self.age(quarter, 75)
        self.age(ancient, 200)

        buckets = self.client.get(RECEIVABLES).json()["buckets"]

        self.assertEqual(buckets["current"], "1500.00")
        self.assertEqual(buckets["31_60"], "1500.00")
        self.assertEqual(buckets["61_90"], "1500.00")
        self.assertEqual(buckets["over_90"], "1500.00")

    def test_bucket_edges(self):
        edges = ((30, "current"), (31, "31_60"), (60, "31_60"),
                 (61, "61_90"), (90, "61_90"), (91, "over_90"))

        for days, bucket in edges:
            sale = self.confirmed(items=[self.line(quantity="1")])
            self.age(sale, days)
            buckets = self.client.get(RECEIVABLES).json()["buckets"]

            self.assertEqual(buckets[bucket], "150.00", days)
            # Clear it so the next edge starts from zero.
            self.pay(sale["id"], "150.00")

    def test_a_returned_balance_is_not_owed(self):
        sale = self.confirmed()
        self.returned(sale, "4")  # takes 600 off what is owed

        self.assertEqual(self.client.get(RECEIVABLES).json()["total"], "900.00")

    def test_biggest_debtor_comes_first(self):
        from apps.sales.models import Customer

        small = Customer.objects.create(business=self.ho, name="Small Buyer")
        self.confirmed()
        self.confirmed(customer=small.pk, items=[self.line(quantity="1")])

        names = [row["name"] for row in self.client.get(RECEIVABLES).json()["parties"]]

        self.assertEqual(names, ["Ram Auto", "Small Buyer"])

    def test_emi_balance_is_kept_apart_from_the_ageing(self):
        sale = self.made_bike(**self.emi_terms())
        self.pay(sale["id"], "20000.00")
        self.confirm(sale["id"])

        report = self.client.get(RECEIVABLES).json()

        self.assertEqual(report["total"], "0.00")
        # 20000 down + 10 x 9000 = 110000, 20000 paid.
        self.assertEqual(report["emi_outstanding"], "90000.00")

    def test_only_the_owner_gets_reports(self):
        self.client.credentials()

        for url in (PROFIT, RECEIVABLES, PAYABLES, STOCK):
            self.assertEqual(self.client.get(url).status_code, 401, url)


class PayablesTests(PurchaseTestCase):
    def age(self, purchase, days):
        Purchase.objects.filter(pk=purchase["id"]).update(purchase_date=days_ago(days))

    def received(self, **extra):
        purchase = self.made(**extra)
        self.assertEqual(self.receive(purchase["id"]).status_code, 200)

        return purchase

    def test_unpaid_received_purchases_are_owed(self):
        purchase = self.received()
        self.pay(purchase["id"], "300.00")

        report = self.client.get(PAYABLES).json()

        self.assertEqual(report["total"], "700.00")
        self.assertEqual(report["parties"][0]["name"], "Himal Traders")

    def test_goods_not_yet_received_are_not_listed(self):
        self.made()

        self.assertEqual(self.client.get(PAYABLES).json()["total"], "0.00")

    def test_it_matches_the_supplier_outstanding_figure(self):
        purchase = self.received()
        self.pay(purchase["id"], "300.00")

        supplier = self.client.get(f"/api/v1/suppliers/{self.supplier.pk}/").json()

        self.assertEqual(self.client.get(PAYABLES).json()["total"], supplier["outstanding"])

    def test_ageing_buckets(self):
        old = self.received()
        fresh = self.received()
        self.age(old, 120)

        buckets = self.client.get(PAYABLES).json()["buckets"]

        self.assertEqual(buckets["over_90"], "1000.00")
        self.assertEqual(buckets["current"], "1000.00")

    def test_paid_purchases_drop_out(self):
        purchase = self.received()
        self.pay(purchase["id"], "1000.00")

        self.assertEqual(self.client.get(PAYABLES).json()["total"], "0.00")


class StockValueTests(PurchaseTestCase):
    def stock_up(self, product, quantity, price):
        Product.objects.filter(pk=product.pk).update(purchase_price=price)
        create_stock_movement(
            user=self.owner, business=self.ho, product=product,
            movement_type=StockMovement.MovementType.OPENING, quantity=quantity,
        )

    def test_value_is_quantity_times_purchase_price(self):
        self.stock_up(self.oil, "10", Decimal("100.00"))
        self.stock_up(self.filter, "5", Decimal("40.00"))

        report = self.client.get(STOCK).json()

        self.assertEqual(report["good_value"], "1200.00")
        self.assertEqual(report["damaged_value"], "0.00")
        self.assertEqual(report["total_value"], "1200.00")
        self.assertEqual(report["good_units"], "15.000")
        # Biggest value first.
        self.assertEqual(report["products"][0]["stock_no"], "OIL-1")

    def test_products_without_a_price_are_counted_not_valued(self):
        self.stock_up(self.oil, "10", Decimal("0.00"))

        report = self.client.get(STOCK).json()

        self.assertEqual(report["total_value"], "0.00")
        self.assertEqual(report["products_without_price"], 1)

    def test_other_businesses_stock_is_not_included(self):
        self.stock_up(self.oil, "10", Decimal("100.00"))
        self.authenticate("OTHER-HO", "owner")

        self.assertEqual(self.client.get(STOCK).json()["total_value"], "0.00")

    def test_empty_stock_gives_zeroes(self):
        report = self.client.get(STOCK).json()

        self.assertEqual(report["total_value"], "0.00")
        self.assertEqual(report["products"], [])
