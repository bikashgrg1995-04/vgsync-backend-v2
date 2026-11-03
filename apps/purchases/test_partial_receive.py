from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.purchases.models import Purchase
from apps.purchases.test_purchases import BASE, SUPPLIERS, dec
from apps.purchases.test_returns import ReturnTestCase

PAYABLES = "/api/v1/reports/payables/"


class PartialReceiveTestCase(ReturnTestCase):
    """Oil 10 x 100 and filters 6 x 100 on one purchase."""

    def ordered(self, **extra):
        return self.made(
            items=[self.line(self.oil, "10"), self.line(self.filter, "6")], **extra
        )

    def deliver(self, purchase, *quantities, **extra):
        """Receive ``quantities`` of the lines in order (None skips a line)."""
        items = [
            {"purchase_item": purchase["items"][index]["id"], "quantity": quantity}
            for index, quantity in enumerate(quantities)
            if quantity is not None
        ]

        return self.client.post(
            f"{BASE}{purchase['id']}/receive/", {"items": items, **extra}, format="json"
        )

    def detail(self, purchase):
        return self.client.get(f"{BASE}{purchase['id']}/").json()

    def supplier_figures(self):
        return self.client.get(f"{SUPPLIERS}{self.supplier.pk}/").json()


class ReceivingInPartsTests(PartialReceiveTestCase):
    def test_a_part_delivery_adds_only_that_stock_and_leaves_the_order_open(self):
        purchase = self.ordered()

        response = self.deliver(purchase, "4", "6")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["status"], "partial")
        self.assertEqual(self.stock(self.oil), dec("4"))
        self.assertEqual(self.stock(self.filter), dec("6"))

        lines = self.detail(purchase)["items"]
        self.assertEqual(lines[0]["received_quantity"], "4.000")
        self.assertEqual(lines[0]["remaining_quantity"], "6.000")
        self.assertEqual(lines[1]["remaining_quantity"], "0.000")

    def test_the_rest_can_arrive_later_and_completes_the_purchase(self):
        purchase = self.ordered()
        self.deliver(purchase, "4")

        response = self.deliver(purchase, "6", "6")

        self.assertEqual(response.json()["status"], "received")
        self.assertEqual(self.stock(self.oil), dec("10"))
        self.assertEqual(self.stock(self.filter), dec("6"))
        self.assertIsNotNone(response.json()["received_at"])

    def test_no_items_receives_everything_still_to_come(self):
        purchase = self.ordered()
        self.deliver(purchase, "4")

        response = self.client.post(f"{BASE}{purchase['id']}/receive/")

        self.assertEqual(response.json()["status"], "received")
        self.assertEqual(self.stock(self.oil), dec("10"))
        self.assertEqual(self.stock(self.filter), dec("6"))

    def test_receiving_everything_at_once_still_works_as_before(self):
        purchase = self.ordered()

        response = self.receive(purchase["id"])

        self.assertEqual(response.json()["status"], "received")
        self.assertEqual(len(self.detail(purchase)["receipts"]), 1)

    def test_each_delivery_is_kept_as_a_receipt(self):
        purchase = self.ordered()
        yesterday = str(timezone.localdate() - timedelta(days=1))
        self.deliver(purchase, "4", received_on=yesterday, notes="First truck")
        self.deliver(purchase, "6", "6")

        receipts = self.detail(purchase)["receipts"]

        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[0]["received_on"], yesterday)
        self.assertEqual(receipts[0]["notes"], "First truck")
        self.assertEqual(receipts[0]["items"][0]["quantity"], "4.000")
        self.assertEqual(
            [row["quantity"] for row in receipts[1]["items"]], ["6.000", "6.000"]
        )

    def test_more_than_is_still_to_come_is_refused_and_nothing_changes(self):
        purchase = self.ordered()
        self.deliver(purchase, "4")

        # The second line is fine; the first is over by one.
        response = self.deliver(purchase, "7", "6")

        self.assertEqual(response.status_code, 400)
        self.assertIn("only 6", str(response.json()))
        self.assertEqual(self.stock(self.oil), dec("4"))
        self.assertEqual(self.stock(self.filter), dec("0"))
        self.assertEqual(self.detail(purchase)["status"], "partial")
        self.assertEqual(len(self.detail(purchase)["receipts"]), 1)

    def test_input_rules(self):
        purchase = self.ordered()
        first = purchase["items"][0]["id"]
        tomorrow = str(timezone.localdate() + timedelta(days=1))

        bad = (
            {"items": []},
            {"items": [{"purchase_item": first, "quantity": "0"}]},
            {"items": [{"purchase_item": first, "quantity": "-1"}]},
            {"items": [{"purchase_item": first, "quantity": "x"}]},
            {"items": [{"purchase_item": 999999, "quantity": "1"}]},
            {"items": [{"purchase_item": first, "quantity": "1"},
                       {"purchase_item": first, "quantity": "1"}]},
            {"received_on": tomorrow},
        )

        for payload in bad:
            response = self.client.post(
                f"{BASE}{purchase['id']}/receive/", payload, format="json"
            )
            self.assertEqual(response.status_code, 400, payload)

        self.assertEqual(self.stock(self.oil), dec("0"))

    def test_a_line_of_another_purchase_is_refused(self):
        purchase = self.ordered()
        other = self.ordered()

        response = self.client.post(
            f"{BASE}{purchase['id']}/receive/",
            {"items": [{"purchase_item": other["items"][0]["id"], "quantity": "1"}]},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stock(self.oil), dec("0"))

    def test_a_fully_received_purchase_cannot_be_received_again(self):
        purchase = self.ordered()
        self.receive(purchase["id"])

        response = self.deliver(purchase, "1")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "invalid_status")
        self.assertEqual(self.stock(self.oil), dec("10"))

    def test_cancelled_purchases_cannot_be_received(self):
        purchase = self.ordered()
        self.client.post(
            f"{BASE}{purchase['id']}/cancel/", {"reason": "x"}, format="json"
        )

        self.assertEqual(self.deliver(purchase, "1").status_code, 409)

    def test_stock_movements_name_the_purchase(self):
        from apps.inventory.models import StockMovement

        purchase = self.ordered()
        self.deliver(purchase, "4")
        self.deliver(purchase, "6", "6")

        moves = StockMovement.objects.filter(
            stock_balance__product=self.oil,
            movement_type=StockMovement.MovementType.PURCHASE_RECEIPT,
        )

        self.assertEqual([m.quantity for m in moves.order_by("id")], [dec("4"), dec("6")])
        self.assertEqual({m.reference for m in moves}, {purchase["purchase_no"]})

    def test_only_a_user_who_may_create_purchases_can_receive(self):
        purchase = self.ordered()
        self.client.credentials()

        response = self.client.post(f"{BASE}{purchase['id']}/receive/")

        self.assertEqual(response.status_code, 401)


class PartlyReceivedPurchaseTests(PartialReceiveTestCase):
    def test_a_partly_received_purchase_cannot_be_edited_or_cancelled(self):
        purchase = self.ordered()
        self.deliver(purchase, "4")

        edited = self.client.patch(
            f"{BASE}{purchase['id']}/", {"notes": "x"}, format="json"
        )
        cancelled = self.client.post(
            f"{BASE}{purchase['id']}/cancel/", {"reason": "x"}, format="json"
        )

        self.assertEqual(edited.status_code, 409)
        self.assertEqual(cancelled.status_code, 409)
        self.assertEqual(self.stock(self.oil), dec("4"))

    def test_the_bill_is_owed_in_full_and_can_be_paid(self):
        purchase = self.ordered()  # 1600
        self.deliver(purchase, "4")

        paid = self.pay(purchase["id"], "500.00")
        figures = self.supplier_figures()

        self.assertEqual(paid.status_code, 201)
        self.assertEqual(figures["total_purchased"], "1600.00")
        self.assertEqual(figures["outstanding"], "1100.00")
        self.assertEqual(figures["credit_to_pay"], "1100.00")
        # Not an advance: the order has started to arrive.
        self.assertEqual(figures["advance_paid"], "0.00")
        self.assertEqual(figures["to_pay_on_order"], "0.00")

    def test_the_supplier_shows_what_is_still_to_come(self):
        purchase = self.ordered()
        self.assertEqual(self.supplier_figures()["stock_pending_delivery"], "0.000")

        # Oil 10 + filters 6 ordered, 4 oil in: 6 oil and 6 filters to come.
        self.deliver(purchase, "4")
        self.assertEqual(self.supplier_figures()["stock_pending_delivery"], "12.000")

        self.deliver(purchase, "6", "6")
        self.assertEqual(self.supplier_figures()["stock_pending_delivery"], "0.000")

    def test_a_draft_stays_an_order_until_something_arrives(self):
        purchase = self.ordered()
        self.pay(purchase["id"], "300.00")

        figures = self.supplier_figures()

        self.assertEqual(figures["advance_paid"], "300.00")
        self.assertEqual(figures["to_pay_on_order"], "1300.00")
        self.assertEqual(figures["outstanding"], "0.00")

    def test_paying_in_advance_then_receiving_in_parts_keeps_the_money_straight(self):
        purchase = self.ordered()
        self.pay(purchase["id"], "300.00")

        self.deliver(purchase, "4")
        figures = self.supplier_figures()

        self.assertEqual(figures["advance_paid"], "0.00")
        self.assertEqual(figures["outstanding"], "1300.00")

    def test_payables_report_includes_the_partly_received_purchase(self):
        purchase = self.ordered()
        self.deliver(purchase, "4")

        report = self.client.get(PAYABLES).json()

        self.assertEqual(report["total"], "1600.00")
        self.assertEqual(report["total"], self.supplier_figures()["outstanding"])

    def test_the_statement_says_partly_received(self):
        purchase = self.ordered()
        self.deliver(purchase, "4")

        rows = self.client.get(f"{SUPPLIERS}{self.supplier.pk}/statement/").json()["results"]

        self.assertIn("partly received", rows[0]["description"])
        self.assertEqual(rows[0]["debit"], "1600.00")


class ReturnsFromPartialTests(PartialReceiveTestCase):
    def test_only_what_arrived_can_be_returned(self):
        purchase = self.ordered()
        self.deliver(purchase, "4")
        line = self.detail(purchase)["items"][0]
        self.assertEqual(line["returnable_quantity"], "4.000")

        too_many = self.send_back(purchase, "5")
        fine = self.send_back(purchase, "4")

        self.assertEqual(too_many.status_code, 400)
        self.assertIn("only 4", str(too_many.json()))
        self.assertEqual(fine.status_code, 201, fine.data)
        self.assertEqual(self.stock(self.oil), dec("0"))

    def test_more_can_be_returned_once_more_arrives(self):
        purchase = self.ordered()
        self.deliver(purchase, "4")
        self.returned(purchase, "4")

        self.assertEqual(self.send_back(purchase, "1").status_code, 400)

        self.deliver(purchase, "6", "6")

        self.assertEqual(self.send_back(purchase, "1").status_code, 201)

    def test_a_draft_purchase_still_cannot_be_returned(self):
        purchase = self.ordered()

        self.assertEqual(self.send_back(purchase, "1").status_code, 409)
