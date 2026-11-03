from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.inventory.models import StockBalance, StockMovement
from apps.sales.models import (
    BikeUnit,
    Customer,
    Followup,
    SaleCreditSettlement,
    SaleReturn,
)
from apps.sales.test_bike_maintenance import BikeTestCase, MaintenanceTests
from apps.sales.test_sales import BASE, CUSTOMERS, PAYMENTS, dec

RETURNS = "/api/v1/sales/returns/"
CREDITS = "/api/v1/sales/credits/"
INCOME = "/api/v1/finance/income/"


class ReturnTestCase(BikeTestCase):

    def item_id(self, sale, index=0):
        return sale["items"][index]["id"]

    def send_back(self, sale, quantity="4", index=0, **extra):
        line = {
            "sale_item": self.item_id(sale, index),
            "quantity": quantity,
            **extra.pop("line", {}),
        }
        payload = {
            "sale": sale["id"],
            "reason": "Customer changed mind",
            "items": [line],
            **extra,
        }

        return self.client.post(RETURNS, payload, format="json")

    def returned(self, sale, quantity="4", **extra):
        response = self.send_back(sale, quantity, **extra)
        self.assertEqual(response.status_code, 201, response.data)

        return response.json()

    def figures(self, customer=None):
        return self.client.get(f"{CUSTOMERS}{(customer or self.customer).pk}/").json()

    def refund(self, amount, **extra):
        return self.client.post(
            CREDITS,
            {"customer": self.customer.pk, "kind": "refund", "amount": amount,
             "method": "cash", **extra},
            format="json",
        )

    def apply(self, sale_id, amount, **extra):
        return self.client.post(
            CREDITS,
            {"customer": self.customer.pk, "kind": "adjustment", "amount": amount,
             "sale": sale_id, **extra},
            format="json",
        )

    def replace(self, amount, items=None, **extra):
        return self.client.post(
            CREDITS,
            {
                "customer": self.customer.pk, "kind": "replacement", "amount": amount,
                "items": items if items is not None else [
                    {"product": self.filter.pk, "quantity": "2"}
                ],
                **extra,
            },
            format="json",
        )

    def paid_sale(self, **extra):
        sale = self.confirmed(**extra)
        self.pay(sale["id"], sale["amount_due"])

        return self.detail(sale["id"])

    def void_return(self, ret, reason="mistake"):
        return self.client.post(
            f"{RETURNS}{ret['id']}/void/", {"reason": reason}, format="json"
        )


class StockReturnTests(ReturnTestCase):

    def test_a_return_puts_goods_back_in_stock(self):
        sale = self.paid_sale()
        self.assertEqual(self.stock(), dec("40"))

        data = self.returned(sale, "4")

        self.assertEqual(data["return_no"], "SRT-000001")
        self.assertEqual(data["status"], "active")
        self.assertEqual(data["goods_value"], "600.00")
        self.assertEqual(self.stock(), dec("44"))

        movement = StockMovement.objects.get(movement_type="sale_return")
        self.assertEqual(movement.quantity, dec("4"))
        self.assertEqual(movement.reference, "SRT-000001")
        self.assertEqual(movement.balance_before, dec("40"))
        self.assertEqual(movement.balance_after, dec("44"))
        self.assertEqual(movement.created_by, self.owner)

    def test_a_paid_sale_makes_credit_to_pay_and_nothing_comes_off_what_is_owed(self):
        sale = self.paid_sale()

        data = self.returned(sale, "4")

        self.assertEqual(data["applied_amount"], "0.00")
        self.assertEqual(data["credit_amount"], "600.00")
        figures = self.figures()
        self.assertEqual(figures["credit_to_pay"], "600.00")
        self.assertEqual(figures["credit_to_receive"], "0.00")
        self.assertEqual(self.detail(sale["id"])["remaining_amount"], "0.00")

    def test_an_unpaid_balance_is_reduced_first(self):
        sale = self.confirmed()

        data = self.returned(sale, "4")

        self.assertEqual(data["applied_amount"], "600.00")
        self.assertEqual(data["credit_amount"], "0.00")
        detail = self.detail(sale["id"])
        self.assertEqual(detail["returned_amount"], "600.00")
        self.assertEqual(detail["remaining_amount"], "900.00")
        # 600 of the 1500 has been taken off, so it counts as part settled.
        self.assertEqual(detail["payment_status"], "partial")
        figures = self.figures()
        self.assertEqual(figures["credit_to_receive"], "900.00")
        self.assertEqual(figures["credit_to_pay"], "0.00")

    def test_only_what_is_left_over_becomes_credit(self):
        sale = self.confirmed()
        self.pay(sale["id"], "1000.00")

        data = self.returned(sale, "6")

        # 900 of goods: 500 was still owed, 400 is owed back.
        self.assertEqual(data["goods_value"], "900.00")
        self.assertEqual(data["applied_amount"], "500.00")
        self.assertEqual(data["credit_amount"], "400.00")
        self.assertEqual(self.detail(sale["id"])["remaining_amount"], "0.00")
        self.assertEqual(self.detail(sale["id"])["payment_status"], "paid")

    def test_value_includes_the_share_of_discount_and_vat(self):
        sale = self.confirmed(
            items=[self.line(quantity="10", price="100.00")],
            discount_amount="100.00", vat_rate="13",
        )
        self.assertEqual(sale["net_total"], "1017.00")
        self.pay(sale["id"], "1017.00")

        first = self.returned(sale, "4")
        rest = self.returned(sale, "6")

        # 400 - 40 = 360, VAT 46.80
        self.assertEqual(first["credit_amount"], "406.80")
        self.assertEqual(rest["credit_amount"], "610.20")
        self.assertEqual(self.figures()["credit_to_pay"], "1017.00")

    def test_values_never_add_up_to_more_than_the_sale(self):
        sale = self.confirmed(
            items=[self.line(quantity="3", price="33.33")],
            discount_amount="0.01", vat_rate="13",
        )
        self.pay(sale["id"], sale["amount_due"])
        total = Decimal("0")

        for _ in range(3):
            total += dec(self.returned(sale, "1")["credit_amount"])

        self.assertLessEqual(total, dec(sale["net_total"]))

    def test_a_line_can_be_returned_in_parts_until_nothing_is_left(self):
        sale = self.paid_sale()

        self.returned(sale, "4")
        item = self.detail(sale["id"])["items"][0]
        self.assertEqual(item["returned_quantity"], "4.000")
        self.assertEqual(item["returnable_quantity"], "6.000")

        too_many = self.send_back(sale, "6.001")
        self.assertEqual(too_many.status_code, 400)
        self.assertIn("only 6", str(too_many.data["items"]))

        self.returned(sale, "6")

        self.assertEqual(self.send_back(sale, "0.001").status_code, 400)
        self.assertEqual(self.stock(), dec("50"))

    def test_a_line_of_another_sale_is_refused(self):
        first = self.paid_sale()
        second = self.paid_sale()

        response = self.client.post(
            RETURNS,
            {"sale": first["id"], "reason": "x",
             "items": [{"sale_item": self.item_id(second), "quantity": "1"}]},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(SaleReturn.objects.exists())

    def test_a_line_listed_twice_is_refused(self):
        sale = self.paid_sale()
        item = self.item_id(sale)

        response = self.client.post(
            RETURNS,
            {"sale": sale["id"], "reason": "x",
             "items": [{"sale_item": item, "quantity": "6"},
                       {"sale_item": item, "quantity": "6"}]},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stock(), dec("40"))

    def test_only_confirmed_sales_can_be_returned(self):
        draft = self.made()
        cancelled = self.made()
        self.client.post(f"{BASE}{cancelled['id']}/cancel/", {"reason": "x"}, format="json")

        for sale in (draft, cancelled):
            response = self.send_back(sale, "1")

            self.assertEqual(response.status_code, 409, sale["status"])
            self.assertEqual(response.data["code"], "invalid_status")

    def test_goods_can_go_back_into_damaged_stock(self):
        sale = self.paid_sale()

        response = self.send_back(sale, "2", line={"stock_condition": "damaged"})

        self.assertEqual(response.status_code, 201, response.data)
        balance = StockBalance.objects.get(product=self.oil, business=self.ho)
        self.assertEqual(balance.damaged_quantity, dec("2"))
        self.assertEqual(balance.quantity, dec("40"))

    def test_input_rules(self):
        sale = self.paid_sale()

        for bad in (
            {"reason": ""},
            {"reason": "  "},
            {"items": []},
            {"return_date": "2999-01-01"},
        ):
            self.assertEqual(self.send_back(sale, **bad).status_code, 400, bad)

        self.assertEqual(self.send_back(sale, "0").status_code, 400)
        self.assertEqual(self.send_back(sale, "-1").status_code, 400)
        self.assertEqual(
            self.send_back(sale, "1", line={"stock_condition": "scrap"}).status_code, 400
        )
        self.assertEqual(self.stock(), dec("40"))

    def test_numbers_run_on_within_a_business(self):
        sale = self.paid_sale()

        first, second = self.returned(sale, "1"), self.returned(sale, "1")

        self.assertEqual((first["number"], second["number"]), (1, 2))

    def test_returns_are_listed_and_filtered(self):
        one, two = self.paid_sale(), self.paid_sale()
        self.returned(one, "1")
        self.returned(two, "2")

        by_sale = self.client.get(RETURNS, {"sale": two["id"]})
        everything = self.client.get(RETURNS)

        self.assertEqual(by_sale.data["count"], 1)
        self.assertEqual(by_sale.json()["results"][0]["credit_amount"], "300.00")
        self.assertEqual(everything.json()["results"][0]["return_no"], "SRT-000002")

    def test_returns_cannot_be_edited_or_deleted(self):
        ret = self.returned(self.paid_sale(), "1")

        self.assertEqual(
            self.client.patch(f"{RETURNS}{ret['id']}/", {"reason": "x"}, format="json").status_code,
            405,
        )
        self.assertEqual(self.client.delete(f"{RETURNS}{ret['id']}/").status_code, 405)

    def test_a_payment_of_a_returned_sale_cannot_be_voided_until_the_return_is(self):
        sale = self.confirmed()
        payment = self.pay(sale["id"], "1500.00").json()
        ret = self.returned(sale, "4")

        blocked = self.client.post(
            f"{PAYMENTS}{payment['id']}/void/", {"reason": "x"}, format="json"
        )
        self.void_return(ret)
        allowed = self.client.post(
            f"{PAYMENTS}{payment['id']}/void/", {"reason": "x"}, format="json"
        )

        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.data["code"], "returns_exist")
        self.assertEqual(allowed.status_code, 200)

    def test_a_payment_after_a_return_is_capped_by_what_is_still_owed(self):
        sale = self.confirmed()
        self.returned(sale, "4")

        over = self.pay(sale["id"], "900.01")
        exact = self.pay(sale["id"], "900.00")

        self.assertEqual(over.status_code, 400)
        self.assertEqual(exact.status_code, 201)
        self.assertEqual(self.detail(sale["id"])["payment_status"], "paid")

    def test_sales_are_filtered_by_payment_status_with_returns_counted(self):
        sale = self.confirmed()
        self.returned(sale, "10")

        self.assertEqual(self.client.get(BASE, {"payment_status": "paid"}).data["count"], 1)
        self.assertEqual(self.client.get(BASE, {"payment_status": "unpaid"}).data["count"], 0)


class WalkInReturnTests(ReturnTestCase):

    def walk_in(self):
        sale = self.made(customer=None)
        self.pay(sale["id"], "1500.00")
        self.confirm(sale["id"])

        return self.detail(sale["id"])

    def test_a_walk_in_return_is_refunded_straight_away(self):
        sale = self.walk_in()

        data = self.returned(sale, "4", refund_method="cash", reference="R-1")

        self.assertEqual(data["credit_amount"], "600.00")
        self.assertIsNone(data["customer"])
        settlement = SaleCreditSettlement.objects.get()
        self.assertEqual(settlement.kind, "refund")
        self.assertEqual(settlement.amount, dec("600"))
        self.assertEqual(settlement.sale_return_id, data["id"])
        self.assertEqual(self.stock(), dec("44"))

        summary = self.client.get(f"{INCOME}summary/").json()
        self.assertEqual(summary["total"], "1500.00")
        self.assertEqual(summary["sale_refunds"], "600.00")
        self.assertEqual(summary["net_total"], "900.00")

    def test_a_walk_in_return_needs_a_refund_method(self):
        sale = self.walk_in()

        response = self.send_back(sale, "4")

        self.assertEqual(response.status_code, 400)
        self.assertIn("refund_method", response.data)
        self.assertEqual(self.stock(), dec("40"))

    def test_a_customer_return_cannot_be_refunded_with_it(self):
        sale = self.paid_sale()

        response = self.send_back(sale, "4", refund_method="cash")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(SaleReturn.objects.exists())

    def test_voiding_a_walk_in_return_voids_its_refund(self):
        sale = self.walk_in()
        ret = self.returned(sale, "4", refund_method="cash")

        response = self.void_return(ret)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(SaleCreditSettlement.objects.get().status, "void")
        self.assertEqual(self.stock(), dec("40"))
        summary = self.client.get(f"{INCOME}summary/").json()
        self.assertEqual(summary["net_total"], "1500.00")

    def test_the_refund_of_a_walk_in_return_cannot_be_voided_on_its_own(self):
        sale = self.walk_in()
        self.returned(sale, "4", refund_method="cash")
        settlement = SaleCreditSettlement.objects.get()

        response = self.client.post(
            f"{CREDITS}{settlement.pk}/void/", {"reason": "x"}, format="json"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "return_refund_locked")


class CreditTests(ReturnTestCase):

    def test_a_refund_uses_up_credit_and_reduces_net_income(self):
        sale = self.paid_sale()
        self.returned(sale, "4")

        response = self.refund("600.00", reference="TXN-5")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.json()["kind"], "refund")
        self.assertEqual(self.figures()["credit_to_pay"], "0.00")

        summary = self.client.get(f"{INCOME}summary/").json()
        self.assertEqual(summary["total"], "1500.00")
        self.assertEqual(summary["sale_refunds"], "600.00")
        self.assertEqual(summary["net_total"], "900.00")

    def test_a_return_alone_does_not_touch_income(self):
        sale = self.paid_sale()
        self.returned(sale, "4")

        summary = self.client.get(f"{INCOME}summary/").json()

        self.assertEqual(summary["sale_refunds"], "0.00")
        self.assertEqual(summary["net_total"], "1500.00")

    def test_refunds_follow_the_summary_filters(self):
        sale = self.paid_sale()
        self.returned(sale, "4")
        self.refund("100.00")

        later = self.client.get(f"{INCOME}summary/", {"income_date__gte": "2999-01-01"})
        by_category = self.client.get(f"{INCOME}summary/", {"category": "stock_sale"})

        self.assertEqual(later.json()["sale_refunds"], "0.00")
        self.assertEqual(by_category.json()["sale_refunds"], "0.00")

    def test_refund_rules(self):
        sale = self.paid_sale()
        self.returned(sale, "4")

        self.assertEqual(self.refund("600.01").status_code, 400)
        self.assertEqual(self.refund("0").status_code, 400)
        self.assertEqual(self.refund("10", method="").status_code, 400)
        self.assertEqual(self.refund("10", method="barter").status_code, 400)
        self.assertEqual(self.refund("10", sale=sale["id"]).status_code, 400)
        self.assertEqual(self.refund("10", settled_on="2999-01-01").status_code, 400)
        self.assertEqual(self.figures()["credit_to_pay"], "600.00")

        self.assertEqual(self.refund("250.00").status_code, 201)
        self.assertEqual(self.refund("350.00").status_code, 201)
        self.assertEqual(self.refund("0.01").status_code, 400)

    def test_credit_can_be_set_against_another_sale(self):
        paid = self.paid_sale()
        self.returned(paid, "4")
        other = self.confirmed(items=[self.line(quantity="2", price="100.00")])

        response = self.apply(other["id"], "150.00")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.json()["sale_no"], other["sale_no"])
        self.assertEqual(self.figures()["credit_to_pay"], "450.00")

        detail = self.detail(other["id"])
        self.assertEqual(detail["credit_applied"], "150.00")
        self.assertEqual(detail["remaining_amount"], "50.00")
        self.assertEqual(detail["payment_status"], "partial")
        self.assertEqual(self.figures()["credit_to_receive"], "50.00")

    def test_credit_set_against_a_sale_closes_its_credit_follow_up(self):
        paid = self.paid_sale()
        self.returned(paid, "4")
        other = self.confirmed(items=[self.line(quantity="2", price="100.00")])
        self.assertEqual(len(self.followups(kind="credit_due", status="pending")), 1)

        self.apply(other["id"], "200.00")

        self.assertEqual(len(self.followups(kind="credit_due", status="pending")), 0)

    def test_applied_credit_rules(self):
        old = self.paid_sale()
        self.returned(old, "4")
        small = self.confirmed(items=[self.line(quantity="1", price="100.00")])
        emi = self.made_bike(**self.emi_terms())
        cancelled = self.made()
        self.client.post(f"{BASE}{cancelled['id']}/cancel/", {"reason": "x"}, format="json")

        self.assertEqual(self.apply(small["id"], "100.01").status_code, 400)
        self.assertEqual(
            self.client.post(
                CREDITS, {"customer": self.customer.pk, "kind": "adjustment", "amount": "10"},
                format="json",
            ).status_code,
            400,
        )
        self.assertEqual(self.apply(small["id"], "10", method="cash").status_code, 400)
        self.assertEqual(self.apply(cancelled["id"], "10").status_code, 409)
        self.assertEqual(self.apply(emi["id"], "10").status_code, 400)

    def test_credit_of_another_customer_cannot_be_set_against_this_sale(self):
        paid = self.paid_sale()
        self.returned(paid, "4")
        stranger = Customer.objects.create(business=self.ho, name="Stranger")

        response = self.client.post(
            CREDITS,
            {"customer": stranger.pk, "kind": "adjustment", "amount": "10", "sale": paid["id"]},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_voiding_applied_credit_restores_credit_and_what_is_owed(self):
        paid = self.paid_sale()
        self.returned(paid, "4")
        other = self.confirmed(items=[self.line(quantity="2", price="100.00")])
        settlement = self.apply(other["id"], "200.00").json()

        response = self.client.post(
            f"{CREDITS}{settlement['id']}/void/", {"reason": "wrong sale"}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.figures()["credit_to_pay"], "600.00")
        self.assertEqual(self.detail(other["id"])["remaining_amount"], "200.00")
        self.assertEqual(len(self.followups(kind="credit_due", status="pending")), 1)

        again = self.client.post(
            f"{CREDITS}{settlement['id']}/void/", {"reason": "x"}, format="json"
        )
        self.assertEqual(again.status_code, 409)

    def test_a_draft_with_applied_credit_cannot_be_cancelled_or_cut_below_it(self):
        paid = self.paid_sale()
        self.returned(paid, "4")
        draft = self.made(items=[self.line(quantity="2", price="100.00")])
        self.apply(draft["id"], "200.00")

        cancel = self.client.post(
            f"{BASE}{draft['id']}/cancel/", {"reason": "x"}, format="json"
        )
        cut = self.client.patch(
            f"{BASE}{draft['id']}/", {"discount_amount": "50.00"}, format="json"
        )

        self.assertEqual(cancel.status_code, 409)
        self.assertEqual(cut.status_code, 400)

    def test_settlements_are_listed_and_filtered(self):
        sale = self.paid_sale()
        self.returned(sale, "6")
        other = self.confirmed(items=[self.line(quantity="2", price="100.00")])
        self.refund("100.00")
        self.apply(other["id"], "200.00")

        self.assertEqual(self.client.get(CREDITS, {"kind": "refund"}).data["count"], 1)
        self.assertEqual(self.client.get(CREDITS).data["count"], 2)

    def test_settlements_cannot_be_edited_or_deleted(self):
        sale = self.paid_sale()
        self.returned(sale, "4")
        settlement = self.refund("10").json()

        self.assertEqual(
            self.client.patch(f"{CREDITS}{settlement['id']}/", {"amount": "1"}, format="json").status_code,
            405,
        )
        self.assertEqual(self.client.delete(f"{CREDITS}{settlement['id']}/").status_code, 405)


class ReplacementTests(ReturnTestCase):

    def test_a_replacement_uses_credit_and_becomes_stock_to_give(self):
        sale = self.paid_sale()
        self.returned(sale, "4")

        response = self.replace("160.00")

        self.assertEqual(response.status_code, 201, response.data)
        data = response.json()
        self.assertEqual(data["delivery_status"], "pending")
        self.assertEqual(len(data["items"]), 1)
        figures = self.figures()
        self.assertEqual(figures["credit_to_pay"], "440.00")
        self.assertEqual(figures["stock_to_give"], "2.000")
        # Nothing has left stock yet.
        self.assertEqual(self.stock(self.filter), dec("50"))

    def test_delivering_takes_the_goods_out_of_stock_once(self):
        sale = self.paid_sale()
        self.returned(sale, "4")
        settlement = self.replace("160.00").json()

        response = self.client.post(f"{CREDITS}{settlement['id']}/deliver/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["delivery_status"], "delivered")
        self.assertEqual(self.stock(self.filter), dec("48"))
        self.assertEqual(self.figures()["stock_to_give"], "0.000")

        movement = StockMovement.objects.get(reference=f"RPL-{settlement['id']:06d}")
        self.assertEqual(movement.movement_type, "sale_issue")
        self.assertEqual(movement.quantity, dec("2"))

        again = self.client.post(f"{CREDITS}{settlement['id']}/deliver/")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.stock(self.filter), dec("48"))

    def test_different_goods_can_be_given_instead(self):
        sale = self.paid_sale()
        self.returned(sale, "4")

        response = self.replace(
            "600.00",
            items=[{"product": self.filter.pk, "quantity": "3"},
                   {"product": self.oil.pk, "quantity": "2"}],
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.figures()["stock_to_give"], "5.000")

    def test_not_enough_stock_changes_nothing(self):
        sale = self.paid_sale()
        self.returned(sale, "4")
        settlement = self.replace(
            "100.00", items=[{"product": self.filter.pk, "quantity": "51"}]
        ).json()

        response = self.client.post(f"{CREDITS}{settlement['id']}/deliver/")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stock(self.filter), dec("50"))
        self.assertEqual(
            self.client.get(f"{CREDITS}{settlement['id']}/").json()["delivery_status"],
            "pending",
        )

    def test_a_pending_replacement_can_be_voided_to_get_the_credit_back(self):
        sale = self.paid_sale()
        self.returned(sale, "4")
        settlement = self.replace("160.00").json()

        response = self.client.post(
            f"{CREDITS}{settlement['id']}/void/", {"reason": "Changed mind"}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.figures()["credit_to_pay"], "600.00")
        self.assertEqual(self.figures()["stock_to_give"], "0.000")

    def test_a_delivered_replacement_cannot_be_voided_or_delivered_when_void(self):
        sale = self.paid_sale()
        self.returned(sale, "4")
        delivered = self.replace("160.00").json()
        voided = self.replace("100.00").json()
        self.client.post(f"{CREDITS}{delivered['id']}/deliver/")
        self.client.post(f"{CREDITS}{voided['id']}/void/", {"reason": "x"}, format="json")

        blocked = self.client.post(
            f"{CREDITS}{delivered['id']}/void/", {"reason": "x"}, format="json"
        )
        late = self.client.post(f"{CREDITS}{voided['id']}/deliver/")

        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.data["code"], "already_delivered")
        self.assertEqual(late.status_code, 409)

    def test_replacement_rules(self):
        sale = self.paid_sale()
        self.returned(sale, "4")

        self.assertEqual(self.replace("600.01").status_code, 400)
        self.assertEqual(self.replace("10", items=[]).status_code, 400)
        self.assertEqual(
            self.replace("10", items=[{"product": self.filter.pk, "quantity": "1"},
                                      {"product": self.filter.pk, "quantity": "1"}]).status_code,
            400,
        )
        self.assertEqual(self.replace("10", method="cash").status_code, 400)
        self.assertEqual(self.replace("10", sale=sale["id"]).status_code, 400)
        self.assertEqual(
            self.replace("10", items=[{"product": self.other_product.pk, "quantity": "1"}]).status_code,
            400,
        )
        self.assertEqual(self.figures()["credit_to_pay"], "600.00")

    def test_only_a_replacement_can_be_delivered(self):
        sale = self.paid_sale()
        self.returned(sale, "4")
        refund = self.refund("10").json()

        self.assertEqual(self.client.post(f"{CREDITS}{refund['id']}/deliver/").status_code, 409)

    def test_pending_replacements_can_be_listed(self):
        sale = self.paid_sale()
        self.returned(sale, "4")
        pending = self.replace("100.00").json()
        done = self.replace("100.00").json()
        self.client.post(f"{CREDITS}{done['id']}/deliver/")

        listed = self.client.get(CREDITS, {"pending": "true"})

        self.assertEqual(listed.data["count"], 1)
        self.assertEqual(listed.json()["results"][0]["id"], pending["id"])

    def test_delivery_date_cannot_be_in_the_future(self):
        sale = self.paid_sale()
        self.returned(sale, "4")
        settlement = self.replace("100.00").json()

        response = self.client.post(
            f"{CREDITS}{settlement['id']}/deliver/",
            {"delivered_on": "2999-01-01"}, format="json",
        )

        self.assertEqual(response.status_code, 400)


class VoidReturnTests(ReturnTestCase):

    def test_voiding_a_return_takes_the_goods_out_again_and_restores_what_is_owed(self):
        sale = self.confirmed()
        ret = self.returned(sale, "4")
        self.assertEqual(self.detail(sale["id"])["remaining_amount"], "900.00")

        response = self.void_return(ret, "Customer kept it")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["status"], "void")
        self.assertEqual(self.stock(), dec("40"))
        self.assertEqual(self.detail(sale["id"])["remaining_amount"], "1500.00")
        self.assertEqual(
            self.detail(sale["id"])["items"][0]["returnable_quantity"], "10.000"
        )
        movement = StockMovement.objects.filter(reference="SRT-000001").first()
        self.assertEqual(movement.movement_type, "adjustment_out")
        # The history stays.
        self.assertEqual(self.client.get(f"{RETURNS}{ret['id']}/").status_code, 200)
        # The same goods can be returned again.
        self.assertEqual(self.send_back(sale, "10").status_code, 201)

    def test_voiding_removes_the_credit(self):
        sale = self.paid_sale()
        ret = self.returned(sale, "4")

        self.void_return(ret)

        self.assertEqual(self.figures()["credit_to_pay"], "0.00")

    def test_a_return_whose_credit_was_used_cannot_be_voided(self):
        sale = self.paid_sale()
        ret = self.returned(sale, "4")
        settlement = self.refund("300.00").json()

        blocked = self.void_return(ret)
        self.client.post(f"{CREDITS}{settlement['id']}/void/", {"reason": "x"}, format="json")
        allowed = self.void_return(ret)

        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.data["code"], "credit_in_use")
        self.assertEqual(allowed.status_code, 200)

    def test_goods_already_sold_again_cannot_be_taken_back_out(self):
        sale = self.paid_sale()
        ret = self.returned(sale, "4")
        StockBalance.objects.filter(product=self.oil).update(quantity=dec("1"))

        response = self.void_return(ret)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(SaleReturn.objects.get().status, "active")

    def test_a_return_is_voided_once_and_needs_a_reason(self):
        ret = self.returned(self.paid_sale(), "4")

        self.assertEqual(
            self.client.post(f"{RETURNS}{ret['id']}/void/", {}, format="json").status_code, 400
        )
        self.void_return(ret)
        again = self.void_return(ret)

        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.stock(), dec("40"))


class ServiceChargeTests(ReturnTestCase):

    maintenance = MaintenanceTests.maintenance
    made_maintenance = MaintenanceTests.made_maintenance

    def maintenance_sale(self):
        sale = self.made_maintenance()
        self.pay(sale["id"], "800.00")
        self.confirm(sale["id"])

        return self.detail(sale["id"])

    def test_parts_of_a_maintenance_sale_can_be_returned(self):
        sale = self.maintenance_sale()

        data = self.returned(sale, "1", index=0)

        self.assertEqual(data["credit_amount"], "150.00")
        self.assertEqual(self.stock(), dec("49"))

    def test_a_service_charge_cannot_be_returned(self):
        sale = self.maintenance_sale()

        response = self.send_back(sale, "1", index=1)

        self.assertEqual(response.status_code, 400)
        self.assertIn("service charge", str(response.data["items"]))
        self.assertEqual(self.detail(sale["id"])["items"][1]["returnable_quantity"], "0.000")

    def test_a_service_charge_does_not_stop_the_discount_share(self):
        sale = self.made_maintenance(discount_amount="80.00")
        self.pay(sale["id"], "720.00")
        self.confirm(sale["id"])
        sale = self.detail(sale["id"])

        data = self.returned(sale, "2")

        # 300 of 800 -> share of the 80 discount is 30
        self.assertEqual(data["credit_amount"], "270.00")


class BikeReturnTests(ReturnTestCase):

    def bike_paid(self):
        sale = self.made_bike()
        self.pay(sale["id"], "100000.00")
        self.confirm(sale["id"])

        return self.detail(sale["id"])

    def test_a_bike_goes_back_into_stock_and_can_be_sold_again(self):
        sale = self.bike_paid()
        unit_id = sale["items"][0]["bike_unit"]
        self.assertEqual(self.stock_of(self.model), dec("4"))

        data = self.returned(sale, "1")

        self.assertEqual(data["credit_amount"], "100000.00")
        self.assertEqual(data["items"][0]["chassis_no"], "CH-001")
        self.assertEqual(BikeUnit.objects.get(pk=unit_id).status, "in_stock")
        self.assertEqual(self.stock_of(self.model), dec("5"))

        again = self.client.post(
            BASE,
            {"sale_type": "bike", "customer": self.customer.pk,
             "items": [{"bike_unit": unit_id}]},
            format="json",
        )
        self.assertEqual(again.status_code, 201, again.data)

    def test_a_bike_is_returned_as_a_whole_into_good_stock(self):
        sale = self.bike_paid()

        partial = self.send_back(sale, "0.5")
        damaged = self.send_back(sale, "1", line={"stock_condition": "damaged"})

        self.assertEqual(partial.status_code, 400)
        self.assertEqual(damaged.status_code, 400)
        self.assertEqual(BikeUnit.objects.get().status, "sold")

    def test_a_bike_cannot_be_returned_twice(self):
        sale = self.bike_paid()
        self.returned(sale, "1")

        self.assertEqual(self.send_back(sale, "1").status_code, 400)

    def test_voiding_a_bike_return_marks_it_sold_again(self):
        sale = self.bike_paid()
        ret = self.returned(sale, "1")

        response = self.void_return(ret)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(BikeUnit.objects.get().status, "sold")
        self.assertEqual(self.stock_of(self.model), dec("4"))

    def test_a_bike_sold_to_someone_else_cannot_be_taken_back(self):
        sale = self.bike_paid()
        ret = self.returned(sale, "1")
        unit_id = sale["items"][0]["bike_unit"]
        new = self.client.post(
            BASE,
            {"sale_type": "bike", "customer": self.customer.pk,
             "items": [{"bike_unit": unit_id}]},
            format="json",
        ).json()
        self.pay(new["id"], "100000.00")
        self.confirm(new["id"])

        response = self.void_return(ret)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "bike_unavailable")
        self.assertEqual(SaleReturn.objects.get().status, "active")

    def emi_sale(self):
        sale = self.made_bike(**self.emi_terms())
        self.pay(sale["id"], "20000.00")
        self.confirm(sale["id"])
        self.pay(sale["id"], "9000.00")

        return self.detail(sale["id"])

    def test_returning_an_emi_bike_ends_the_sale_and_everything_paid_becomes_credit(self):
        sale = self.emi_sale()
        self.assertEqual(sale["amount_due"], "110000.00")

        data = self.returned(sale, "1")

        # 110000 - 29000 still to come is dropped; 29000 received is owed back.
        self.assertEqual(data["applied_amount"], "81000.00")
        self.assertEqual(data["credit_amount"], "29000.00")
        detail = self.detail(sale["id"])
        self.assertEqual(detail["remaining_amount"], "0.00")
        self.assertEqual(detail["payment_status"], "paid")
        states = [r["status"] for r in detail["installments"]]
        self.assertEqual(states[0], "paid")
        self.assertEqual(set(states[1:]), {"cancelled"})
        figures = self.figures()
        self.assertEqual(figures["credit_to_pay"], "29000.00")
        self.assertEqual(figures["credit_to_receive"], "0.00")

    def test_the_follow_ups_of_an_ended_emi_sale_close_and_open_again_with_the_void(self):
        sale = self.emi_sale()
        self.assertEqual(len(self.followups(kind="emi_due", status="pending")), 9)

        ret = self.returned(sale, "1")
        self.assertEqual(len(self.followups(kind="emi_due", status="pending")), 0)

        self.void_return(ret)

        self.assertEqual(len(self.followups(kind="emi_due", status="pending")), 9)
        detail = self.detail(sale["id"])
        self.assertEqual(detail["remaining_amount"], "81000.00")
        self.assertEqual(detail["installments"][0]["status"], "paid")
        self.assertNotIn("cancelled", {r["status"] for r in detail["installments"]})
        self.assertEqual(self.figures()["credit_to_pay"], "0.00")

    def test_the_credit_of_an_ended_emi_sale_can_be_refunded(self):
        sale = self.emi_sale()
        self.returned(sale, "1")

        self.assertEqual(self.refund("29000.00", method="bank").status_code, 201)
        self.assertEqual(self.figures()["credit_to_pay"], "0.00")
        self.assertEqual(self.refund("0.01", method="bank").status_code, 400)


class ReturnAccessTests(ReturnTestCase):

    def staff(self, username, **perms):
        position = self.make_position(self.ho, f"P-{username}", **perms)

        return self.make_user(
            username, User.Role.STAFF, company=self.company, position=position
        )

    def test_staff_follow_their_position(self):
        sale = self.paid_sale()
        ret = self.returned(sale, "4")
        self.staff("viewer", sales=("view",))
        self.staff("seller", sales=("view", "create"))

        self.authenticate("ACME-HO", "viewer")
        self.assertEqual(self.client.get(RETURNS).status_code, 200)
        self.assertEqual(self.client.get(CREDITS).status_code, 200)
        self.assertEqual(self.send_back(sale, "1").status_code, 403)
        self.assertEqual(self.refund("10").status_code, 403)
        self.assertEqual(self.void_return(ret).status_code, 403)
        self.assertEqual(self.client.post(f"{CREDITS}1/deliver/").status_code, 403)

        self.authenticate("ACME-HO", "seller")
        self.assertEqual(self.send_back(sale, "1").status_code, 201)

    def test_staff_without_the_module_are_refused(self):
        self.staff("buyer", purchases=("view", "create"))
        self.authenticate("ACME-HO", "buyer")

        for url in (RETURNS, CREDITS):
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_another_company_sees_nothing_and_changes_nothing(self):
        sale = self.paid_sale()
        ret = self.returned(sale, "4")
        settlement = self.refund("10").json()

        self.authenticate("OTHER-HO", "owner")

        self.assertEqual(self.client.get(RETURNS).data["count"], 0)
        self.assertEqual(self.client.get(f"{RETURNS}{ret['id']}/").status_code, 404)
        self.assertEqual(self.void_return(ret).status_code, 404)
        self.assertEqual(self.send_back(sale, "1").status_code, 400)
        self.assertEqual(self.client.get(CREDITS).data["count"], 0)
        self.assertEqual(
            self.client.post(f"{CREDITS}{settlement['id']}/void/", {"reason": "x"}, format="json").status_code,
            404,
        )

    def test_company_owner_is_read_only_in_a_branch(self):
        self.authenticate("ACME-BR", "owner")

        self.assertEqual(self.client.get(RETURNS).status_code, 200)
        self.assertEqual(self.client.post(RETURNS, {}, format="json").status_code, 403)
        self.assertEqual(self.client.post(CREDITS, {}, format="json").status_code, 403)

    def test_unauthenticated_requests_are_rejected(self):
        self.client.credentials()

        for url in (RETURNS, CREDITS):
            self.assertEqual(self.client.get(url).status_code, 401, url)


class ReturnModelTests(ReturnTestCase):

    def test_database_keeps_a_settlement_in_line_with_its_kind(self):
        base = dict(
            business=self.ho, customer=self.customer, amount=Decimal("10"),
            settled_on=timezone.localdate(),
        )
        sale = self.paid_sale()

        with self.assertRaises(IntegrityError), transaction.atomic():
            SaleCreditSettlement.objects.create(kind="adjustment", **base)

        with self.assertRaises(IntegrityError), transaction.atomic():
            SaleCreditSettlement.objects.create(
                kind="refund", method="cash", sale_id=sale["id"], **base
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            SaleCreditSettlement.objects.create(kind="refund", **base)

        with self.assertRaises(IntegrityError), transaction.atomic():
            SaleCreditSettlement.objects.create(
                kind="refund", method="cash", **{**base, "amount": Decimal("0")}
            )

    def test_without_a_customer_only_a_walk_in_refund_is_possible(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            SaleCreditSettlement.objects.create(
                business=self.ho, customer=None, kind="refund", method="cash",
                amount=Decimal("10"), settled_on=timezone.localdate(),
            )

    def test_database_refuses_a_duplicate_return_number(self):
        sale = self.paid_sale()
        self.returned(sale, "1")
        saved = SaleReturn.objects.get()

        with self.assertRaises(IntegrityError), transaction.atomic():
            SaleReturn.objects.create(
                business=self.ho, number=saved.number, sale_id=sale["id"],
                customer=self.customer, return_date=timezone.localdate(), reason="x",
            )
