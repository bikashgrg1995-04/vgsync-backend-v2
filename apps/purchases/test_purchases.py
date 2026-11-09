from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.common.testing import TenantTestMixin
from apps.finance.models import Expense
from apps.inventory.models import Product, StockBalance, StockMovement
from apps.purchases.models import Purchase, PurchaseItem, PurchasePayment, Supplier

BASE = "/api/v1/purchases/"
SUPPLIERS = "/api/v1/suppliers/"
PAYMENTS = "/api/v1/purchases/payments/"
EXPENSES = "/api/v1/finance/expenses/"


def dec(value):
    return Decimal(str(value))


class PurchaseTestCase(TenantTestMixin, APITestCase):

    def setUp(self):
        super().setUp()

        self.oil = Product.objects.create(
            owner_business=self.ho, stock_no="OIL-1", name="Engine Oil", unit="ltr"
        )
        self.filter = Product.objects.create(
            owner_business=self.ho, stock_no="FLT-1", name="Oil Filter"
        )
        self.other_product = Product.objects.create(
            owner_business=self.other_ho, stock_no="X-1", name="Other Product"
        )

        self.supplier = Supplier.objects.create(business=self.ho, name="Himal Traders")
        self.other_supplier = Supplier.objects.create(
            business=self.other_ho, name="Other Traders"
        )

        self.authenticate("ACME-HO", "owner")

    # -- helpers -----------------------------------------------------------

    def line(self, product=None, quantity="10", price="100.00"):
        return {
            "product": (product or self.oil).pk,
            "quantity": quantity,
            "unit_price": price,
        }

    def create(self, items=None, **extra):
        payload = {
            "supplier": self.supplier.pk,
            "items": items if items is not None else [self.line()],
            **extra,
        }

        return self.client.post(BASE, payload, format="json")

    def made(self, **extra):
        response = self.create(**extra)
        self.assertEqual(response.status_code, 201, response.data)

        return response.json()

    def receive(self, purchase_id):
        return self.client.post(f"{BASE}{purchase_id}/receive/")

    def pay(self, purchase_id, amount, **extra):
        return self.client.post(
            PAYMENTS,
            {"purchase": purchase_id, "amount": amount, "method": "cash", **extra},
            format="json",
        )

    def stock(self, product=None, business=None):
        balance = StockBalance.objects.filter(
            product=product or self.oil, business=business or self.ho
        ).first()

        return balance.quantity if balance else Decimal("0")


class TotalsTests(PurchaseTestCase):

    def test_totals_follow_the_documented_order(self):
        data = self.made(
            items=[
                self.line(self.oil, "10", "100.00"),
                self.line(self.filter, "3", "250.50"),
            ],
            discount_amount="151.50",
            vat_rate="13",
            freight_amount="200.00",
        )

        # 1000.00 + 751.50 = 1751.50; minus 151.50 discount = 1600.00;
        # VAT 13% = 208.00; plus 200.00 freight (untaxed) = 2008.00
        self.assertEqual(data["subtotal"], "1751.50")
        self.assertEqual(data["vat_amount"], "208.00")
        self.assertEqual(data["net_total"], "2008.00")
        self.assertEqual([i["line_total"] for i in data["items"]], ["1000.00", "751.50"])

    def test_vat_defaults_to_zero_and_is_never_added_silently(self):
        data = self.made()

        self.assertEqual(data["vat_rate"], "0.00")
        self.assertEqual(data["vat_amount"], "0.00")
        self.assertEqual(data["net_total"], "1000.00")

    def test_amounts_are_rounded_to_the_paisa(self):
        data = self.made(
            items=[self.line(quantity="3", price="33.33")], vat_rate="13"
        )

        # 99.99 -> VAT 12.9987 -> 13.00
        self.assertEqual(data["subtotal"], "99.99")
        self.assertEqual(data["vat_amount"], "13.00")
        self.assertEqual(data["net_total"], "112.99")

    def test_totals_sent_by_the_client_are_ignored(self):
        data = self.made(net_total="1.00", subtotal="1.00", vat_amount="999.00")

        self.assertEqual(data["net_total"], "1000.00")

    def test_discount_cannot_exceed_the_subtotal(self):
        response = self.create(discount_amount="1000.01")

        self.assertEqual(response.status_code, 400)
        self.assertIn("discount_amount", response.data)
        self.assertEqual(self.create(discount_amount="1000.00").status_code, 201)

    def test_line_and_header_rules(self):
        for bad in (
            {"items": []},
            {"items": [self.line(quantity="0")]},
            {"items": [self.line(quantity="-1")]},
            {"items": [self.line(price="0")]},
            {"items": [self.line(price="-5")]},
            {"discount_amount": "-1"},
            {"freight_amount": "-1"},
            {"vat_rate": "101"},
            {"vat_rate": "-1"},
            {"purchase_date": "2999-01-01"},
        ):
            self.assertEqual(self.create(**bad).status_code, 400, bad)

        self.assertFalse(Purchase.objects.exists())

    def test_money_is_returned_as_strings(self):
        data = self.made()

        for field in ("subtotal", "net_total", "paid_amount", "remaining_amount"):
            self.assertIsInstance(data[field], str, field)

        self.assertEqual(data["status"], "draft")
        self.assertEqual(data["purchase_no"], "PUR-000001")
        self.assertEqual(data["purchase_date"], str(timezone.localdate()))


class CreateAndEditTests(PurchaseTestCase):

    def test_numbers_run_on_within_a_business(self):
        first = self.made()
        second = self.made()

        self.assertEqual((first["number"], second["number"]), (1, 2))
        self.assertEqual(second["purchase_no"], "PUR-000002")

    def test_supplier_and_products_must_belong_to_the_business(self):
        wrong_supplier = self.client.post(
            BASE, {"supplier": self.other_supplier.pk, "items": [self.line()]},
            format="json",
        )
        wrong_product = self.create(items=[self.line(self.other_product)])

        self.assertEqual(wrong_supplier.status_code, 400)
        self.assertIn("supplier", wrong_supplier.data)
        self.assertEqual(wrong_product.status_code, 400)

    def test_inactive_suppliers_and_products_cannot_be_chosen(self):
        Supplier.objects.filter(pk=self.supplier.pk).update(is_active=False)
        self.assertEqual(self.create().status_code, 400)

        Supplier.objects.filter(pk=self.supplier.pk).update(is_active=True)
        Product.objects.filter(pk=self.oil.pk).update(is_active=False)
        self.assertEqual(self.create().status_code, 400)

    def test_the_same_supplier_bill_cannot_be_entered_twice(self):
        self.made(bill_no="B-77")

        again = self.create(bill_no="B-77")

        self.assertEqual(again.status_code, 400)
        self.assertIn("bill_no", again.data)

    def test_a_cancelled_bill_can_be_entered_again(self):
        first = self.made(bill_no="B-77")
        self.client.post(f"{BASE}{first['id']}/cancel/", {"reason": "wrong"}, format="json")

        self.assertEqual(self.create(bill_no="B-77").status_code, 201)

    def test_editing_a_draft_replaces_lines_and_recalculates(self):
        purchase = self.made(vat_rate="13")

        response = self.client.patch(
            f"{BASE}{purchase['id']}/",
            {"items": [self.line(self.filter, "2", "500.00")], "freight_amount": "50.00"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        data = response.json()
        self.assertEqual([i["product"] for i in data["items"]], [self.filter.pk])
        # 1000.00 + 13% = 1130.00 + 50.00
        self.assertEqual(data["net_total"], "1180.00")

    def test_editing_only_the_header_keeps_the_lines(self):
        purchase = self.made()

        response = self.client.patch(
            f"{BASE}{purchase['id']}/",
            {"discount_amount": "100.00", "notes": "Festival offer"},
            format="json",
        )

        data = response.json()
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["net_total"], "900.00")
        self.assertEqual(data["notes"], "Festival offer")

    def test_a_draft_cannot_be_edited_below_what_is_already_paid(self):
        purchase = self.made()
        self.pay(purchase["id"], "800.00")

        response = self.client.patch(
            f"{BASE}{purchase['id']}/", {"discount_amount": "500.00"}, format="json"
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            dec(self.client.get(f"{BASE}{purchase['id']}/").json()["net_total"]),
            dec("1000"),
        )

    def test_received_and_cancelled_purchases_are_locked(self):
        received = self.made()
        self.receive(received["id"])
        cancelled = self.made()
        self.client.post(f"{BASE}{cancelled['id']}/cancel/", {"reason": "x"}, format="json")

        for purchase in (received, cancelled):
            response = self.client.patch(
                f"{BASE}{purchase['id']}/", {"notes": "x"}, format="json"
            )

            self.assertEqual(response.status_code, 409, purchase)
            self.assertEqual(response.data["code"], "document_not_editable")

    def test_purchases_are_never_deleted_or_replaced(self):
        purchase = self.made()

        self.assertEqual(self.client.delete(f"{BASE}{purchase['id']}/").status_code, 405)
        self.assertEqual(
            self.client.put(f"{BASE}{purchase['id']}/", {}, format="json").status_code, 405
        )


class ReceiveStockTests(PurchaseTestCase):

    def test_drafts_do_not_touch_stock(self):
        self.made()

        self.assertEqual(self.stock(), 0)

    def test_receiving_adds_every_line_to_stock_once(self):
        purchase = self.made(
            items=[self.line(self.oil, "10.500"), self.line(self.filter, "4")]
        )

        response = self.receive(purchase["id"])

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["status"], "received")
        self.assertEqual(self.stock(self.oil), dec("10.500"))
        self.assertEqual(self.stock(self.filter), dec("4"))

        movement = StockMovement.objects.get(stock_balance__product=self.oil)
        self.assertEqual(movement.movement_type, "purchase_receipt")
        self.assertEqual(movement.reference, "PUR-000001")
        self.assertEqual(movement.created_by, self.owner)
        self.assertEqual(movement.balance_before, 0)
        self.assertEqual(movement.balance_after, dec("10.500"))

    def test_damaged_goods_go_to_damaged_stock(self):
        purchase = self.made(items=[self.line(self.oil, "10")])
        line = purchase["items"][0]["id"]

        response = self.client.post(
            f"{BASE}{purchase['id']}/receive/",
            {"items": [{"purchase_item": line, "quantity": "10", "damaged_quantity": "3"}]},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["status"], "received")

        balance = StockBalance.objects.get(product=self.oil, business=self.ho)
        self.assertEqual(balance.quantity, dec("7"))
        self.assertEqual(balance.damaged_quantity, dec("3"))

        conditions = {
            m.stock_condition: m.quantity
            for m in StockMovement.objects.filter(stock_balance=balance)
        }
        self.assertEqual(conditions, {"good": dec("7"), "damaged": dec("3")})

        item = response.json()["receipts"][0]["items"][0]
        self.assertEqual(item["quantity"], "10.000")
        self.assertEqual(item["damaged_quantity"], "3.000")

    def test_everything_arriving_damaged_leaves_good_stock_alone(self):
        purchase = self.made(items=[self.line(self.oil, "4")])
        line = purchase["items"][0]["id"]

        response = self.client.post(
            f"{BASE}{purchase['id']}/receive/",
            {"items": [{"purchase_item": line, "quantity": "4", "damaged_quantity": "4"}]},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        balance = StockBalance.objects.get(product=self.oil, business=self.ho)
        self.assertEqual(balance.quantity, 0)
        self.assertEqual(balance.damaged_quantity, dec("4"))
        self.assertEqual(response.json()["status"], "received")

    def test_damaged_cannot_be_more_than_received(self):
        purchase = self.made(items=[self.line(self.oil, "5")])
        line = purchase["items"][0]["id"]

        response = self.client.post(
            f"{BASE}{purchase['id']}/receive/",
            {"items": [{"purchase_item": line, "quantity": "2", "damaged_quantity": "3"}]},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(StockMovement.objects.exists())
        self.assertEqual(Purchase.objects.get().status, "draft")

    def test_receiving_twice_never_double_counts(self):
        purchase = self.made()
        self.receive(purchase["id"])

        again = self.receive(purchase["id"])

        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.data["code"], "invalid_status")
        self.assertEqual(self.stock(), dec("10"))
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_if_one_line_fails_nothing_is_received(self):
        purchase = self.made(items=[self.line(self.oil), self.line(self.filter)])
        Product.objects.filter(pk=self.filter.pk).update(is_active=False)

        response = self.receive(purchase["id"])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stock(self.oil), 0)
        self.assertFalse(StockMovement.objects.exists())
        self.assertEqual(Purchase.objects.get().status, "draft")
        self.assertIsNone(Purchase.objects.get().received_at)

    def test_receiving_records_who_and_when(self):
        purchase = self.made()
        self.receive(purchase["id"])

        saved = Purchase.objects.get(pk=purchase["id"])
        self.assertEqual(saved.received_by, self.owner)
        self.assertIsNotNone(saved.received_at)

    def test_a_cancelled_purchase_cannot_be_received(self):
        purchase = self.made()
        self.client.post(f"{BASE}{purchase['id']}/cancel/", {"reason": "x"}, format="json")

        self.assertEqual(self.receive(purchase["id"]).status_code, 409)
        self.assertEqual(self.stock(), 0)

    def test_a_branch_buys_for_its_own_stock_from_the_head_office_catalog(self):
        self.make_user("bowner", User.Role.OWNER, business=self.branch)
        branch_supplier = Supplier.objects.create(business=self.branch, name="Local Shop")
        self.authenticate("ACME-BR", "bowner")

        response = self.client.post(
            BASE,
            {"supplier": branch_supplier.pk, "items": [self.line(self.oil, "5")]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.json()["purchase_no"], "PUR-000001")

        self.receive(response.json()["id"])

        self.assertEqual(self.stock(self.oil, self.branch), dec("5"))
        self.assertEqual(self.stock(self.oil, self.ho), 0)


class CancelTests(PurchaseTestCase):

    def test_a_draft_is_cancelled_with_a_reason(self):
        purchase = self.made()

        response = self.client.post(
            f"{BASE}{purchase['id']}/cancel/", {"reason": "Ordered elsewhere"}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.data)
        data = response.json()
        self.assertEqual(data["status"], "cancelled")
        self.assertEqual(data["cancel_reason"], "Ordered elsewhere")
        saved = Purchase.objects.get()
        self.assertEqual(saved.cancelled_by, self.owner)

    def test_a_reason_is_required(self):
        purchase = self.made()

        self.assertEqual(
            self.client.post(f"{BASE}{purchase['id']}/cancel/", {}, format="json").status_code,
            400,
        )

    def test_a_received_purchase_cannot_be_cancelled(self):
        purchase = self.made()
        self.receive(purchase["id"])

        response = self.client.post(
            f"{BASE}{purchase['id']}/cancel/", {"reason": "x"}, format="json"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.stock(), dec("10"))

    def test_payments_must_be_voided_before_cancelling(self):
        purchase = self.made()
        payment = self.pay(purchase["id"], "100.00").json()

        blocked = self.client.post(
            f"{BASE}{purchase['id']}/cancel/", {"reason": "x"}, format="json"
        )
        self.client.post(f"{PAYMENTS}{payment['id']}/void/", {"reason": "x"}, format="json")
        allowed = self.client.post(
            f"{BASE}{purchase['id']}/cancel/", {"reason": "x"}, format="json"
        )

        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.data["code"], "payments_exist")
        self.assertEqual(allowed.status_code, 200)


class PaymentTests(PurchaseTestCase):

    def test_a_new_purchase_is_unpaid(self):
        data = self.made()

        self.assertEqual(data["payment_status"], "unpaid")
        self.assertEqual(data["paid_amount"], "0.00")
        self.assertEqual(data["remaining_amount"], "1000.00")

    def test_part_then_full_payment(self):
        purchase = self.made()
        self.receive(purchase["id"])

        first = self.pay(purchase["id"], "400.00", reference="CHQ-1", method="cheque")
        self.assertEqual(first.status_code, 201, first.data)

        part = self.client.get(f"{BASE}{purchase['id']}/").json()
        self.assertEqual(part["payment_status"], "partial")
        self.assertEqual(part["paid_amount"], "400.00")
        self.assertEqual(part["remaining_amount"], "600.00")

        self.pay(purchase["id"], "600.00")

        full = self.client.get(f"{BASE}{purchase['id']}/").json()
        self.assertEqual(full["payment_status"], "paid")
        self.assertEqual(full["remaining_amount"], "0.00")
        self.assertEqual(len(full["payments"]), 2)

    def test_paying_more_than_is_owed_is_refused(self):
        purchase = self.made()
        self.pay(purchase["id"], "700.00")

        response = self.pay(purchase["id"], "300.01")

        self.assertEqual(response.status_code, 400)
        self.assertIn("amount", response.data)
        self.assertEqual(PurchasePayment.objects.count(), 1)

    def test_payment_rules(self):
        purchase = self.made()

        self.assertEqual(self.pay(purchase["id"], "0").status_code, 400)
        self.assertEqual(self.pay(purchase["id"], "-1").status_code, 400)
        self.assertEqual(self.pay(purchase["id"], "10", method="barter").status_code, 400)
        self.assertEqual(
            self.pay(purchase["id"], "10", paid_on="2999-01-01").status_code, 400
        )
        self.assertFalse(PurchasePayment.objects.exists())

    def test_an_advance_can_be_paid_before_the_goods_arrive(self):
        purchase = self.made()

        response = self.pay(purchase["id"], "250.00")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.stock(), 0)

    def test_a_cancelled_purchase_cannot_be_paid(self):
        purchase = self.made()
        self.client.post(f"{BASE}{purchase['id']}/cancel/", {"reason": "x"}, format="json")

        self.assertEqual(self.pay(purchase["id"], "10").status_code, 409)

    def test_voiding_a_payment_puts_the_amount_back(self):
        purchase = self.made()
        payment = self.pay(purchase["id"], "1000.00").json()

        response = self.client.post(
            f"{PAYMENTS}{payment['id']}/void/", {"reason": "bounced"}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["status"], "void")
        data = self.client.get(f"{BASE}{purchase['id']}/").json()
        self.assertEqual(data["payment_status"], "unpaid")
        self.assertEqual(data["remaining_amount"], "1000.00")
        # The history stays.
        self.assertEqual(len(data["payments"]), 1)

        again = self.client.post(
            f"{PAYMENTS}{payment['id']}/void/", {"reason": "x"}, format="json"
        )
        self.assertEqual(again.status_code, 409)

    def test_a_reason_is_needed_to_void(self):
        purchase = self.made()
        payment = self.pay(purchase["id"], "10.00").json()

        self.assertEqual(
            self.client.post(f"{PAYMENTS}{payment['id']}/void/", {}, format="json").status_code,
            400,
        )

    def test_payments_cannot_be_edited_or_deleted(self):
        purchase = self.made()
        payment = self.pay(purchase["id"], "10.00").json()

        self.assertEqual(
            self.client.patch(f"{PAYMENTS}{payment['id']}/", {"amount": "1"}, format="json").status_code,
            405,
        )
        self.assertEqual(self.client.delete(f"{PAYMENTS}{payment['id']}/").status_code, 405)

    def test_a_payment_also_becomes_a_purchase_expense(self):
        purchase = self.made()
        payment = self.pay(purchase["id"], "400.00", reference="CHQ-9", method="cheque").json()

        expense = Expense.objects.get()
        self.assertEqual(expense.category, "purchase")
        self.assertEqual(expense.amount, dec("400"))
        self.assertEqual(expense.business, self.ho)
        self.assertEqual(expense.purchase_payment_id, payment["id"])
        self.assertEqual(expense.payment_method, "cheque")
        self.assertIn("Himal Traders", expense.description)
        self.assertIn("PUR-000001", expense.description)

        self.client.post(f"{PAYMENTS}{payment['id']}/void/", {"reason": "x"}, format="json")
        self.assertEqual(Expense.objects.get().status, "void")

    def test_purchase_expenses_cannot_be_changed_by_hand(self):
        purchase = self.made()
        self.pay(purchase["id"], "400.00")
        expense_id = Expense.objects.get().pk

        patched = self.client.patch(f"{EXPENSES}{expense_id}/", {"amount": "1"}, format="json")
        voided = self.client.post(
            f"{EXPENSES}{expense_id}/void/", {"reason": "x"}, format="json"
        )
        manual = self.client.post(
            EXPENSES, {"category": "purchase", "amount": "10"}, format="json"
        )

        self.assertEqual(patched.status_code, 409)
        self.assertEqual(voided.status_code, 409)
        self.assertEqual(manual.status_code, 400)

    def test_payments_can_be_listed_and_filtered(self):
        one = self.made()
        two = self.made()
        self.pay(one["id"], "100.00")
        self.pay(two["id"], "200.00")

        by_purchase = self.client.get(PAYMENTS, {"purchase": two["id"]})

        self.assertEqual(by_purchase.data["count"], 1)
        self.assertEqual(by_purchase.json()["results"][0]["amount"], "200.00")
        self.assertEqual(by_purchase.json()["results"][0]["purchase_no"], "PUR-000002")


class ListAndFilterTests(PurchaseTestCase):

    def test_filters(self):
        paid = self.made(bill_no="A")
        partial = self.made(bill_no="B")
        unpaid = self.made(bill_no="C")
        self.pay(paid["id"], "1000.00")
        self.pay(partial["id"], "100.00")
        self.receive(paid["id"])

        def ids(**query):
            response = self.client.get(BASE, query)
            self.assertEqual(response.status_code, 200)

            return {r["id"] for r in response.json()["results"]}

        self.assertEqual(ids(payment_status="paid"), {paid["id"]})
        self.assertEqual(ids(payment_status="partial"), {partial["id"]})
        self.assertEqual(ids(payment_status="unpaid"), {unpaid["id"]})
        self.assertEqual(ids(status="received"), {paid["id"]})
        self.assertEqual(ids(supplier=self.supplier.pk), {paid["id"], partial["id"], unpaid["id"]})
        self.assertEqual(ids(search="B"), {partial["id"]})
        self.assertEqual(ids(purchase_date__gte="2999-01-01"), set())

    def test_the_list_is_paginated_newest_first_with_totals(self):
        self.made()
        self.made()

        response = self.client.get(BASE)

        self.assertEqual(response.data["count"], 2)
        results = response.json()["results"]
        self.assertEqual([r["number"] for r in results], [2, 1])
        self.assertEqual(results[0]["net_total"], "1000.00")
        self.assertEqual(results[0]["paid_amount"], "0.00")


class SupplierTests(PurchaseTestCase):

    def test_create_edit_and_deactivate(self):
        created = self.client.post(
            SUPPLIERS,
            {"name": " VG Parts ", "phone": "9800000000", "pan_number": "123456789"},
            format="json",
        )

        self.assertEqual(created.status_code, 201, created.data)
        supplier_id = created.json()["id"]
        self.assertEqual(created.json()["name"], "VG Parts")
        self.assertEqual(created.json()["outstanding"], "0.00")

        edited = self.client.patch(
            f"{SUPPLIERS}{supplier_id}/", {"address": "Kathmandu"}, format="json"
        )
        deleted = self.client.delete(f"{SUPPLIERS}{supplier_id}/")

        self.assertEqual(edited.status_code, 200)
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(Supplier.objects.get(pk=supplier_id).is_active)

    def test_names_are_unique_per_business_ignoring_case(self):
        duplicate = self.client.post(SUPPLIERS, {"name": "himal traders"}, format="json")

        self.assertEqual(duplicate.status_code, 400)

        # Another business may use the same name.
        self.authenticate("OTHER-HO", "owner")
        self.assertEqual(
            self.client.post(SUPPLIERS, {"name": "Himal Traders"}, format="json").status_code,
            201,
        )

    def test_what_is_owed_to_a_supplier(self):
        received = self.made(items=[self.line(quantity="10", price="100.00")])
        self.receive(received["id"])
        self.pay(received["id"], "300.00")

        later = self.made(items=[self.line(quantity="5", price="100.00")])
        self.pay(later["id"], "200.00")

        cancelled = self.made()
        self.client.post(f"{BASE}{cancelled['id']}/cancel/", {"reason": "x"}, format="json")

        data = self.client.get(f"{SUPPLIERS}{self.supplier.pk}/").json()

        # Only received purchases are owed: 1000 - 300.
        self.assertEqual(data["total_purchased"], "1000.00")
        self.assertEqual(data["outstanding"], "700.00")
        # 200 already paid on the purchase still waiting for its goods.
        self.assertEqual(data["advance_paid"], "200.00")

    def test_voided_payments_do_not_count(self):
        purchase = self.made()
        self.receive(purchase["id"])
        payment = self.pay(purchase["id"], "400.00").json()
        self.client.post(f"{PAYMENTS}{payment['id']}/void/", {"reason": "x"}, format="json")

        data = self.client.get(f"{SUPPLIERS}{self.supplier.pk}/").json()

        self.assertEqual(data["outstanding"], "1000.00")

    def test_list_search_and_filter(self):
        Supplier.objects.create(business=self.ho, name="Old Co", is_active=False)

        active = self.client.get(SUPPLIERS, {"is_active": "true"})
        found = self.client.get(SUPPLIERS, {"search": "himal"})

        self.assertEqual(active.data["count"], 1)
        self.assertEqual(found.data["count"], 1)


class AccessTests(PurchaseTestCase):

    def staff(self, username, business=None, **perms):
        business = business or self.ho
        position = self.make_position(business, f"P-{username}", **perms)
        kwargs = (
            {"company": self.company} if business == self.ho else {"business": business}
        )

        return self.make_user(username, User.Role.STAFF, position=position, **kwargs)

    def test_staff_follow_their_position(self):
        self.staff("viewer", purchases=("view",))
        self.staff("buyer", purchases=("view", "create"))
        self.make_user("nopos", User.Role.STAFF, company=self.company)
        purchase = self.made()

        self.authenticate("ACME-HO", "viewer")
        self.assertEqual(self.client.get(BASE).status_code, 200)
        self.assertEqual(self.create().status_code, 403)
        self.assertEqual(self.receive(purchase["id"]).status_code, 403)
        self.assertEqual(self.pay(purchase["id"], "10").status_code, 403)

        self.authenticate("ACME-HO", "buyer")
        self.assertEqual(self.create().status_code, 201)

        # Without an active position the account cannot even sign in.
        self.assertEqual(self.login("ACME-HO", "nopos").status_code, 400)

    def test_staff_without_the_module_are_refused(self):
        self.staff("sales", sales=("view",))
        self.authenticate("ACME-HO", "sales")

        for url in (BASE, SUPPLIERS, PAYMENTS):
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_accountants_and_admins_can_work_with_purchases(self):
        self.make_user("adm", User.Role.ADMIN, company=self.company)

        self.authenticate("ACME-HO", "adm")

        self.assertEqual(self.create().status_code, 201)

    def test_unauthenticated_requests_are_rejected(self):
        self.client.credentials()

        self.assertEqual(self.client.get(BASE).status_code, 401)

    def test_another_company_sees_nothing_and_changes_nothing(self):
        purchase = self.made()

        self.authenticate("OTHER-HO", "owner")

        self.assertEqual(self.client.get(BASE).data["count"], 0)
        self.assertEqual(self.client.get(f"{BASE}{purchase['id']}/").status_code, 404)
        self.assertEqual(self.receive(purchase["id"]).status_code, 404)
        self.assertEqual(
            self.client.patch(f"{BASE}{purchase['id']}/", {"notes": "x"}, format="json").status_code,
            404,
        )
        self.assertEqual(self.pay(purchase["id"], "10").status_code, 400)
        self.assertEqual(self.client.get(f"{SUPPLIERS}{self.supplier.pk}/").status_code, 404)

    def test_company_owner_is_read_only_in_a_branch(self):
        self.authenticate("ACME-BR", "owner")

        self.assertEqual(self.client.get(BASE).status_code, 200)
        self.assertEqual(
            self.client.post(SUPPLIERS, {"name": "X"}, format="json").status_code, 403
        )

    def test_the_head_office_purchases_are_not_visible_in_the_branch(self):
        self.made()

        self.authenticate("ACME-BR", "owner")

        self.assertEqual(self.client.get(BASE).data["count"], 0)


class ModelTests(PurchaseTestCase):

    def test_database_refuses_bad_amounts(self):
        purchase = Purchase.objects.create(
            business=self.ho, number=1, supplier=self.supplier,
            purchase_date=timezone.localdate(),
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            PurchaseItem.objects.create(
                purchase=purchase, product=self.oil, quantity=Decimal("0"),
                unit_price=Decimal("1"), line_total=Decimal("1"),
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            PurchasePayment.objects.create(
                purchase=purchase, amount=Decimal("0"),
                paid_on=timezone.localdate(), method="cash",
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Purchase.objects.filter(pk=purchase.pk).update(
                subtotal=Decimal("10"), discount_amount=Decimal("11")
            )

    def test_database_refuses_a_duplicate_number_in_a_business(self):
        Purchase.objects.create(
            business=self.ho, number=1, supplier=self.supplier,
            purchase_date=timezone.localdate(),
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Purchase.objects.create(
                business=self.ho, number=1, supplier=self.supplier,
                purchase_date=timezone.localdate(),
            )
