from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.purchases.models import Purchase
from apps.purchases.test_purchases import PurchaseTestCase
from apps.payroll.test_payroll import PayrollTestCase
from apps.inventory.models import LocalProduct
from apps.sales.models import Customer, Sale
from apps.sales.test_sale_returns import ReturnTestCase

TREND = "/api/v1/reports/sales-trend/"
PRODUCTS = "/api/v1/reports/top-products/"
CUSTOMERS = "/api/v1/reports/top-customers/"
SUPPLIERS = "/api/v1/reports/top-suppliers/"
PAYROLL = "/api/v1/reports/payroll-summary/"


def days_ago(days):
    return timezone.localdate() - timedelta(days=days)


class SalesAnalyticsCase(ReturnTestCase):
    def sold_on(self, days, **extra):
        sale = self.confirmed(**extra)
        Sale.objects.filter(pk=sale["id"]).update(sale_date=days_ago(days))

        return sale


class SalesTrendTests(SalesAnalyticsCase):
    def test_sales_are_totalled_by_month(self):
        self.sold_on(0)
        self.sold_on(0, items=[self.line(self.oil, "2")])
        self.sold_on(70)

        report = self.client.get(TREND).json()

        self.assertEqual(report["group_by"], "month")
        self.assertEqual(report["sales"], 3)
        # 1500 + 300 + 1500
        self.assertEqual(report["total"], "3300.00")
        self.assertEqual(report["net"], "3300.00")
        self.assertGreaterEqual(len(report["periods"]), 2)
        this_month = timezone.localdate().strftime("%Y-%m")
        row = next(p for p in report["periods"] if p["period"] == this_month)
        self.assertEqual(row["sales"], 2)
        self.assertEqual(row["total"], "1800.00")

    def test_returns_are_taken_off(self):
        sale = self.sold_on(0)
        self.returned(sale, "4")  # 600

        report = self.client.get(TREND).json()

        self.assertEqual(report["total"], "1500.00")
        self.assertEqual(report["returns"], "600.00")
        self.assertEqual(report["net"], "900.00")

    def test_a_voided_return_is_not_counted(self):
        sale = self.sold_on(0)
        ret = self.returned(sale, "4")
        self.void_return(ret)

        self.assertEqual(self.client.get(TREND).json()["returns"], "0.00")

    def test_drafts_and_cancelled_sales_are_left_out(self):
        self.made()
        gone = self.made()
        self.client.post(
            f"/api/v1/sales/{gone['id']}/cancel/", {"reason": "x"}, format="json"
        )

        self.assertEqual(self.client.get(TREND).json()["sales"], 0)

    def test_daily_and_weekly_grouping(self):
        self.sold_on(0, items=[self.line(self.oil, "1")])
        self.sold_on(1, items=[self.line(self.oil, "1")])

        daily = self.client.get(TREND, {"group_by": "day"}).json()
        weekly = self.client.get(TREND, {"group_by": "week"}).json()

        self.assertEqual(len(daily["periods"]), 2)
        self.assertEqual(daily["periods"][0]["period"], str(days_ago(1)))
        self.assertLessEqual(len(weekly["periods"]), 2)
        self.assertEqual(weekly["total"], "300.00")

    def test_date_range(self):
        self.sold_on(0, items=[self.line(self.oil, "1")])
        self.sold_on(40, items=[self.line(self.oil, "1")])

        report = self.client.get(TREND, {"date_from": str(days_ago(5))}).json()

        self.assertEqual(report["sales"], 1)

    def test_bad_input_is_refused(self):
        for params in (
            {"group_by": "year"},
            {"date_from": "2026-13-01"},
            {"date_from": "2026-05-02", "date_to": "2026-05-01"},
        ):
            self.assertEqual(self.client.get(TREND, params).status_code, 400, params)

    def test_only_the_active_business(self):
        self.sold_on(0)
        self.authenticate("OTHER-HO", "owner")

        self.assertEqual(self.client.get(TREND).json()["sales"], 0)


class TopProductsTests(SalesAnalyticsCase):
    def setUp(self):
        super().setUp()
        self.wash = LocalProduct.objects.create(
            business=self.ho, name="Bike Wash", sale_price=Decimal("300.00")
        )

    def test_ranked_by_revenue_by_default(self):
        self.confirmed(items=[self.line(self.oil, "10"), self.line(self.filter, "20")])

        rows = self.client.get(PRODUCTS).json()["products"]

        # Oil 10 x 150 = 1500, filter 20 x 80 = 1600.
        self.assertEqual([r["stock_no"] for r in rows], ["FLT-1", "OIL-1"])
        self.assertEqual(rows[0]["revenue"], "1600.00")
        self.assertEqual(rows[0]["quantity"], "20.000")

    def test_ranked_by_quantity(self):
        self.confirmed(items=[self.line(self.oil, "30"), self.line(self.filter, "5")])

        report = self.client.get(PRODUCTS, {"by": "quantity"}).json()

        self.assertEqual(report["by"], "quantity")
        self.assertEqual(report["products"][0]["stock_no"], "OIL-1")

    def test_returns_reduce_the_net_quantity(self):
        sale = self.confirmed()
        self.returned(sale, "4")

        row = self.client.get(PRODUCTS).json()["products"][0]

        self.assertEqual(row["quantity"], "10.000")
        self.assertEqual(row["returned_quantity"], "4.000")
        self.assertEqual(row["net_quantity"], "6.000")

    def test_local_products_are_listed_under_their_name(self):
        self.confirmed(
            items=[{"local_product": self.wash.pk, "quantity": "3"}]
        )

        row = self.client.get(PRODUCTS).json()["products"][0]

        self.assertEqual(row["name"], "Bike Wash")
        self.assertEqual(row["local_product"], self.wash.pk)
        self.assertIsNone(row["product"])
        self.assertEqual(row["revenue"], "900.00")

    def test_sales_add_up_across_orders_and_the_limit_applies(self):
        self.confirmed(items=[self.line(self.oil, "5")])
        self.confirmed(items=[self.line(self.oil, "5")])
        self.confirmed(items=[self.line(self.filter, "1")])

        report = self.client.get(PRODUCTS, {"limit": 1}).json()

        self.assertEqual(len(report["products"]), 1)
        self.assertEqual(report["products"][0]["quantity"], "10.000")

    def test_drafts_are_not_counted(self):
        self.made()

        self.assertEqual(self.client.get(PRODUCTS).json()["products"], [])

    def test_bad_input_is_refused(self):
        for params in ({"by": "profit"}, {"limit": "0"}, {"limit": "500"}, {"limit": "x"}):
            self.assertEqual(self.client.get(PRODUCTS, params).status_code, 400, params)


class TopCustomersTests(SalesAnalyticsCase):
    def test_customers_ranked_by_what_they_bought(self):
        small = Customer.objects.create(business=self.ho, name="Small Buyer")
        self.confirmed()
        self.confirmed(customer=small.pk, items=[self.line(self.oil, "1")])

        report = self.client.get(CUSTOMERS).json()

        self.assertEqual([c["name"] for c in report["customers"]], ["Ram Auto", "Small Buyer"])
        self.assertEqual(report["customers"][0]["total"], "1500.00")
        self.assertEqual(report["customers"][0]["sales"], 1)

    def test_returns_are_shown_per_customer(self):
        sale = self.confirmed()
        self.returned(sale, "4")

        row = self.client.get(CUSTOMERS).json()["customers"][0]

        self.assertEqual(row["returns"], "600.00")

    def test_walk_in_sales_are_totalled_apart(self):
        # A walk-in sale is paid in full before it is confirmed.
        sale = self.made(customer=None)
        self.assertEqual(self.pay(sale["id"], "1500.00").status_code, 201)
        self.assertEqual(self.confirm(sale["id"]).status_code, 200)

        report = self.client.get(CUSTOMERS).json()

        self.assertEqual(report["customers"], [])
        self.assertEqual(report["walk_in"], {"sales": 1, "total": "1500.00"})

    def test_limit_and_date_range(self):
        small = Customer.objects.create(business=self.ho, name="Small Buyer")
        self.confirmed()
        self.sold = self.confirmed(customer=small.pk, items=[self.line(self.oil, "1")])
        Sale.objects.filter(pk=self.sold["id"]).update(sale_date=days_ago(60))

        recent = self.client.get(CUSTOMERS, {"date_from": str(days_ago(10))}).json()
        top = self.client.get(CUSTOMERS, {"limit": 1}).json()

        self.assertEqual([c["name"] for c in recent["customers"]], ["Ram Auto"])
        self.assertEqual(len(top["customers"]), 1)


class TopSuppliersTests(PurchaseTestCase):
    def received(self, price="100.00", quantity="10"):
        purchase = self.made(items=[self.line(self.oil, quantity, price)])
        self.assertEqual(self.receive(purchase["id"]).status_code, 200)

        return purchase

    def test_suppliers_ranked_by_what_was_bought(self):
        from apps.purchases.models import Supplier

        small = Supplier.objects.create(business=self.ho, name="Small Traders")
        self.received()
        self.received()
        bought = self.made(
            supplier=small.pk, items=[self.line(self.oil, "1", "50.00")]
        )
        self.receive(bought["id"])

        rows = self.client.get(SUPPLIERS).json()["suppliers"]

        self.assertEqual([r["name"] for r in rows], ["Himal Traders", "Small Traders"])
        self.assertEqual(rows[0]["total"], "2000.00")
        self.assertEqual(rows[0]["purchases"], 2)

    def test_drafts_and_cancelled_purchases_do_not_count_but_partial_ones_do(self):
        self.made()
        partial = self.made(items=[self.line(self.oil, "10"), self.line(self.filter, "5")])
        self.client.post(
            f"{'/api/v1/purchases/'}{partial['id']}/receive/",
            {"items": [{"purchase_item": partial["items"][0]["id"], "quantity": "4"}]},
            format="json",
        )

        rows = self.client.get(SUPPLIERS).json()["suppliers"]

        self.assertEqual(rows[0]["total"], "1500.00")
        self.assertEqual(rows[0]["purchases"], 1)

    def test_date_range_and_limit(self):
        old = self.received()
        Purchase.objects.filter(pk=old["id"]).update(purchase_date=days_ago(90))

        recent = self.client.get(SUPPLIERS, {"date_from": str(days_ago(30))}).json()

        self.assertEqual(recent["suppliers"], [])
        self.assertEqual(self.client.get(SUPPLIERS, {"limit": 0}).status_code, 400)


class PayrollSummaryTests(PayrollTestCase):
    def test_earned_paid_and_owed_per_person(self):
        self.daily(self.sita)
        self.wage(self.sita, "2026-01-01", "1000.00")
        self.entry(self.sita, "bonus", "2026-01-01", "500.00")
        self.entry(self.sita, "deduction", "2026-01-01", "200.00")
        self.pay(self.sita, "800.00")

        report = self.client.get(PAYROLL).json()

        row = next(p for p in report["staff"] if p["name"] == "sita")
        self.assertEqual(row["earned"], "1300.00")
        self.assertEqual(row["paid"], "800.00")
        self.assertEqual(row["owed_now"], "500.00")
        self.assertEqual(report["earned"], "1300.00")
        self.assertEqual(report["paid"], "800.00")

    def test_by_month(self):
        self.daily(self.sita)
        self.wage(self.sita, "2026-01-01", "1000.00")
        self.today(__import__("datetime").date(2026, 2, 3))
        self.wage(self.sita, "2026-02-02", "400.00")
        self.pay(self.sita, "300.00")

        months = {m["month"]: m for m in self.client.get(PAYROLL).json()["by_month"]}

        self.assertEqual(months["2026-01"]["earned"], "1000.00")
        self.assertEqual(months["2026-02"]["earned"], "400.00")
        self.assertEqual(months["2026-02"]["paid"], "300.00")

    def test_the_date_range_limits_earned_and_paid_but_not_what_is_owed(self):
        self.daily(self.sita)
        self.wage(self.sita, "2026-01-01", "1000.00")
        self.today(__import__("datetime").date(2026, 2, 3))
        self.wage(self.sita, "2026-02-02", "400.00")

        report = self.client.get(PAYROLL, {"date_from": "2026-02-01"}).json()

        row = next(p for p in report["staff"] if p["name"] == "sita")
        self.assertEqual(row["earned"], "400.00")
        self.assertEqual(row["owed_now"], "1400.00")

    def test_void_entries_and_payments_are_left_out(self):
        self.daily(self.sita)
        entry = self.wage(self.sita, "2026-01-01", "1000.00").json()
        self.client.post(
            f"/api/v1/payroll/entries/{entry['id']}/void/", {"reason": "x"}, format="json"
        )

        self.assertEqual(self.client.get(PAYROLL).json()["earned"], "0.00")

    def test_nothing_gives_zeroes(self):
        report = self.client.get(PAYROLL).json()

        self.assertEqual(report["staff"], [])
        self.assertEqual(report["owed_now"], "0.00")
