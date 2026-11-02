from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.common.testing import TenantTestMixin
from apps.inventory.models import Product, StockBalance, StockMovement
from apps.inventory.services.stock import create_stock_movement
from apps.sales.models import Customer, Sale

BASE = "/api/v1/sales/"
CUSTOMERS = "/api/v1/sales/customers/"
PAYMENTS = "/api/v1/sales/payments/"


def dec(value):
    return Decimal(str(value))


class SaleTestCase(TenantTestMixin, APITestCase):

    def setUp(self):
        super().setUp()

        self.oil = Product.objects.create(
            owner_business=self.ho, stock_no="OIL-1", name="Engine Oil",
            unit="ltr", sale_price=Decimal("150.00"),
        )
        self.filter = Product.objects.create(
            owner_business=self.ho, stock_no="FLT-1", name="Oil Filter",
            sale_price=Decimal("80.00"),
        )
        self.unpriced = Product.objects.create(
            owner_business=self.ho, stock_no="NP-1", name="No Price"
        )
        self.other_product = Product.objects.create(
            owner_business=self.other_ho, stock_no="X-1", name="Other Product"
        )

        for product in (self.oil, self.filter, self.unpriced):
            create_stock_movement(
                user=self.owner, business=self.ho, product=product,
                movement_type=StockMovement.MovementType.OPENING, quantity="50",
            )

        self.customer = Customer.objects.create(
            business=self.ho, name="Ram Auto", phone="9800000001"
        )
        self.other_customer = Customer.objects.create(
            business=self.other_ho, name="Other Buyer"
        )

        self.authenticate("ACME-HO", "owner")

    # -- helpers -----------------------------------------------------------

    def line(self, product=None, quantity="10", price=None):
        data = {"product": (product or self.oil).pk, "quantity": quantity}

        if price is not None:
            data["unit_price"] = price

        return data

    def create(self, items=None, **extra):
        payload = {
            "customer": self.customer.pk,
            "items": items if items is not None else [self.line()],
            **extra,
        }

        return self.client.post(BASE, payload, format="json")

    def made(self, **extra):
        response = self.create(**extra)
        self.assertEqual(response.status_code, 201, response.data)

        return response.json()

    def confirm(self, sale_id):
        return self.client.post(f"{BASE}{sale_id}/confirm/")

    def confirmed(self, **extra):
        sale = self.made(**extra)
        response = self.confirm(sale["id"])
        self.assertEqual(response.status_code, 200, response.data)

        return response.json()

    def pay(self, sale_id, amount, **extra):
        return self.client.post(
            PAYMENTS,
            {"sale": sale_id, "amount": amount, "method": "cash", **extra},
            format="json",
        )

    def stock(self, product=None):
        return StockBalance.objects.get(
            product=product or self.oil, business=self.ho
        ).quantity

    def detail(self, sale_id):
        return self.client.get(f"{BASE}{sale_id}/").json()

    def figures(self, customer=None):
        return self.client.get(f"{CUSTOMERS}{(customer or self.customer).pk}/").json()


class SaleTotalsTests(SaleTestCase):

    def test_price_defaults_to_the_product_sale_price(self):
        sale = self.made()

        self.assertEqual(sale["items"][0]["unit_price"], "150.00")
        self.assertEqual(sale["items"][0]["line_total"], "1500.00")
        self.assertEqual(sale["net_total"], "1500.00")
        self.assertEqual(sale["status"], "draft")
        self.assertEqual(sale["sale_no"], "SAL-000001")
        self.assertIsNone(sale["invoice_no"])

    def test_the_price_on_a_line_can_be_changed(self):
        sale = self.made(items=[self.line(price="120.00")])

        self.assertEqual(sale["items"][0]["unit_price"], "120.00")
        self.assertEqual(sale["net_total"], "1200.00")
        # The product itself is untouched.
        self.oil.refresh_from_db()
        self.assertEqual(self.oil.sale_price, dec("150.00"))

    def test_a_product_without_a_price_needs_one_on_the_line(self):
        refused = self.create(items=[self.line(self.unpriced)])
        accepted = self.create(items=[self.line(self.unpriced, price="10.00")])

        self.assertEqual(refused.status_code, 400)
        self.assertEqual(accepted.status_code, 201)

    def test_discount_comes_before_vat_and_totals_are_calculated(self):
        sale = self.made(
            items=[self.line(quantity="10", price="100.00"),
                   self.line(self.filter, quantity="2", price="50.00")],
            discount_amount="100.00", vat_rate="13",
        )

        # 1000 + 100 = 1100; - 100 = 1000; VAT 130 -> 1130
        self.assertEqual(sale["subtotal"], "1100.00")
        self.assertEqual(sale["vat_amount"], "130.00")
        self.assertEqual(sale["net_total"], "1130.00")

    def test_vat_is_zero_unless_asked_for(self):
        self.assertEqual(self.made()["vat_amount"], "0.00")

    def test_totals_sent_by_the_client_are_ignored(self):
        sale = self.made(net_total="1.00", subtotal="1.00", vat_amount="99.00")

        self.assertEqual(sale["net_total"], "1500.00")

    def test_bad_input_is_refused(self):
        for payload in (
            {"items": []},
            {"items": [self.line(quantity="0")]},
            {"items": [self.line(price="-1")]},
            {"discount_amount": "1501.00"},
            {"vat_rate": "101"},
            {"sale_date": "2999-01-01"},
            {"customer": self.other_customer.pk},
            {"items": [{"product": self.other_product.pk, "quantity": "1", "unit_price": "5"}]},
        ):
            self.assertEqual(self.create(**payload).status_code, 400, payload)

        self.assertFalse(Sale.objects.exists())

    def test_inactive_customer_cannot_be_chosen(self):
        Customer.objects.filter(pk=self.customer.pk).update(is_active=False)

        self.assertEqual(self.create().status_code, 400)

    def test_numbers_run_on_within_a_business(self):
        first, second = self.made(), self.made()

        self.assertEqual((first["number"], second["number"]), (1, 2))


class SaleEditTests(SaleTestCase):

    def test_a_draft_can_be_edited_and_lines_replaced(self):
        sale = self.made()

        response = self.client.patch(
            f"{BASE}{sale['id']}/",
            {"items": [self.line(self.filter, quantity="3")], "discount_amount": "40.00"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        data = response.json()
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["net_total"], "200.00")

    def test_editing_only_the_discount_keeps_the_lines(self):
        sale = self.made()

        data = self.client.patch(
            f"{BASE}{sale['id']}/", {"discount_amount": "500.00"}, format="json"
        ).json()

        self.assertEqual(data["net_total"], "1000.00")
        self.assertEqual(len(data["items"]), 1)

    def test_a_confirmed_sale_cannot_be_edited(self):
        sale = self.confirmed()

        response = self.client.patch(
            f"{BASE}{sale['id']}/", {"notes": "x"}, format="json"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "document_not_editable")

    def test_a_draft_cannot_be_cut_below_what_was_received(self):
        sale = self.made()
        self.pay(sale["id"], "1000.00")

        response = self.client.patch(
            f"{BASE}{sale['id']}/", {"discount_amount": "600.00"}, format="json"
        )

        self.assertEqual(response.status_code, 400)

    def test_a_draft_can_be_cancelled_with_a_reason(self):
        sale = self.made()

        missing = self.client.post(f"{BASE}{sale['id']}/cancel/", {}, format="json")
        done = self.client.post(
            f"{BASE}{sale['id']}/cancel/", {"reason": "Changed mind"}, format="json"
        )

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(done.json()["status"], "cancelled")
        self.assertEqual(self.confirm(sale["id"]).status_code, 409)

    def test_a_draft_with_a_payment_cannot_be_cancelled(self):
        sale = self.made()
        self.pay(sale["id"], "100.00")

        response = self.client.post(
            f"{BASE}{sale['id']}/cancel/", {"reason": "x"}, format="json"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "payments_exist")

    def test_a_confirmed_sale_cannot_be_cancelled(self):
        sale = self.confirmed()

        response = self.client.post(
            f"{BASE}{sale['id']}/cancel/", {"reason": "x"}, format="json"
        )

        self.assertEqual(response.status_code, 409)


class ConfirmTests(SaleTestCase):

    def test_confirming_takes_the_goods_out_of_stock_and_gives_an_invoice(self):
        sale = self.made()
        self.assertEqual(self.stock(), dec("50"))

        data = self.confirm(sale["id"]).json()

        self.assertEqual(data["status"], "confirmed")
        self.assertEqual(data["invoice_no"], "INV-000001")
        self.assertEqual(self.stock(), dec("40"))

        movement = StockMovement.objects.get(movement_type="sale_issue")
        self.assertEqual(movement.quantity, dec("10"))
        self.assertEqual(movement.reference, "INV-000001")
        self.assertEqual(movement.balance_before, dec("50"))
        self.assertEqual(movement.balance_after, dec("40"))
        self.assertEqual(movement.created_by, self.owner)

    def test_stock_moves_exactly_once(self):
        sale = self.made()
        self.confirm(sale["id"])

        again = self.confirm(sale["id"])

        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.data["code"], "invalid_status")
        self.assertEqual(self.stock(), dec("40"))
        self.assertEqual(StockMovement.objects.filter(movement_type="sale_issue").count(), 1)

    def test_not_enough_stock_changes_nothing(self):
        sale = self.made(items=[self.line(self.filter, quantity="5"),
                                self.line(self.oil, quantity="51")])

        response = self.confirm(sale["id"])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stock(self.filter), dec("50"))
        self.assertEqual(self.stock(), dec("50"))
        self.assertEqual(self.detail(sale["id"])["status"], "draft")
        self.assertIsNone(self.detail(sale["id"])["invoice_no"])

    def test_invoice_numbers_follow_confirmation_without_gaps(self):
        first = self.made()
        cancelled = self.made()
        third = self.made()
        self.client.post(f"{BASE}{cancelled['id']}/cancel/", {"reason": "x"}, format="json")

        self.confirm(third["id"])
        self.confirm(first["id"])

        self.assertEqual(self.detail(third["id"])["invoice_no"], "INV-000001")
        self.assertEqual(self.detail(first["id"])["invoice_no"], "INV-000002")
        self.assertIsNone(self.detail(cancelled["id"])["invoice_no"])

    def test_a_walk_in_sale_must_be_paid_in_full(self):
        sale = self.made(customer=None, walk_in_name="Bike rider")
        self.assertEqual(sale["customer_name"], "Bike rider")

        refused = self.confirm(sale["id"])
        self.pay(sale["id"], "1500.00")
        accepted = self.confirm(sale["id"])

        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.data["code"], "customer_required")
        self.assertEqual(accepted.status_code, 200)

    def test_a_walk_in_sale_paid_in_full_cannot_be_left_unpaid_by_voiding(self):
        sale = self.made(customer=None)
        payment = self.pay(sale["id"], "1500.00").json()
        self.confirm(sale["id"])

        response = self.client.post(
            f"{PAYMENTS}{payment['id']}/void/", {"reason": "x"}, format="json"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "customer_required")

    def test_walk_in_name_defaults_to_walk_in(self):
        self.assertEqual(self.made(customer=None)["customer_name"], "Walk-in")

    def test_sales_without_a_customer_are_allowed(self):
        self.assertEqual(self.made(customer=None)["customer"], None)


class PaymentTests(SaleTestCase):

    def test_part_payments_add_up_and_status_follows(self):
        sale = self.confirmed()

        self.assertEqual(self.detail(sale["id"])["payment_status"], "unpaid")

        first = self.pay(sale["id"], "400.00", reference="TXN-1", method="bank")
        self.assertEqual(first.status_code, 201, first.data)

        detail = self.detail(sale["id"])
        self.assertEqual(detail["paid_amount"], "400.00")
        self.assertEqual(detail["remaining_amount"], "1100.00")
        self.assertEqual(detail["payment_status"], "partial")

        self.pay(sale["id"], "1100.00")
        detail = self.detail(sale["id"])
        self.assertEqual(detail["remaining_amount"], "0.00")
        self.assertEqual(detail["payment_status"], "paid")
        self.assertEqual(len(detail["payments"]), 2)

    def test_a_payment_cannot_exceed_what_is_due(self):
        sale = self.confirmed()
        self.pay(sale["id"], "1000.00")

        over = self.pay(sale["id"], "500.01")
        exact = self.pay(sale["id"], "500.00")

        self.assertEqual(over.status_code, 400)
        self.assertEqual(exact.status_code, 201)

    def test_payment_input_rules(self):
        sale = self.confirmed()

        for bad in (
            {"amount": "0"},
            {"amount": "-5"},
            {"method": "barter"},
            {"paid_on": "2999-01-01"},
        ):
            payload = {"sale": sale["id"], "amount": "10", "method": "cash", **bad}

            self.assertEqual(
                self.client.post(PAYMENTS, payload, format="json").status_code, 400, bad
            )

    def test_a_draft_can_take_an_advance(self):
        sale = self.made()

        self.assertEqual(self.pay(sale["id"], "300.00").status_code, 201)
        self.assertEqual(self.figures()["advance_received"], "300.00")

    def test_a_cancelled_sale_cannot_take_payment(self):
        sale = self.made()
        self.client.post(f"{BASE}{sale['id']}/cancel/", {"reason": "x"}, format="json")

        response = self.pay(sale["id"], "10")

        self.assertEqual(response.status_code, 409)

    def test_voiding_a_payment_puts_the_amount_back_due(self):
        sale = self.confirmed()
        payment = self.pay(sale["id"], "1500.00").json()

        response = self.client.post(
            f"{PAYMENTS}{payment['id']}/void/", {"reason": "Cheque bounced"}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["status"], "void")
        self.assertEqual(self.detail(sale["id"])["remaining_amount"], "1500.00")
        self.assertEqual(self.figures()["credit_to_receive"], "1500.00")

        again = self.client.post(
            f"{PAYMENTS}{payment['id']}/void/", {"reason": "x"}, format="json"
        )
        self.assertEqual(again.status_code, 409)

    def test_void_needs_a_reason(self):
        sale = self.confirmed()
        payment = self.pay(sale["id"], "10").json()

        response = self.client.post(f"{PAYMENTS}{payment['id']}/void/", {}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_payments_cannot_be_edited_or_deleted(self):
        sale = self.confirmed()
        payment = self.pay(sale["id"], "10").json()

        self.assertEqual(
            self.client.patch(f"{PAYMENTS}{payment['id']}/", {"amount": "1"}, format="json").status_code,
            405,
        )
        self.assertEqual(self.client.delete(f"{PAYMENTS}{payment['id']}/").status_code, 405)

    def test_payments_are_listed_and_filtered(self):
        one, two = self.confirmed(), self.confirmed()
        self.pay(one["id"], "100.00")
        self.pay(two["id"], "200.00", method="bank")

        by_sale = self.client.get(PAYMENTS, {"sale": two["id"]})
        by_method = self.client.get(PAYMENTS, {"method": "bank"})

        self.assertEqual(by_sale.data["count"], 1)
        self.assertEqual(by_method.data["count"], 1)
        self.assertEqual(self.client.get(PAYMENTS).data["count"], 2)

    def test_taking_payment_does_not_create_an_expense(self):
        from apps.finance.models import Expense

        sale = self.confirmed()
        self.pay(sale["id"], "100.00")

        self.assertFalse(Expense.objects.exists())


class CustomerTests(SaleTestCase):

    def test_customer_crud_and_scoping(self):
        created = self.client.post(
            CUSTOMERS, {"name": "  New Buyer ", "phone": "9811111111"}, format="json"
        )

        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.json()["name"], "New Buyer")
        self.assertEqual(created.json()["credit_to_receive"], "0.00")

        renamed = self.client.patch(
            f"{CUSTOMERS}{created.json()['id']}/", {"address": "Bharatpur"}, format="json"
        )
        self.assertEqual(renamed.json()["address"], "Bharatpur")

        listed = self.client.get(CUSTOMERS)
        self.assertEqual(listed.data["count"], 2)
        self.assertEqual(
            self.client.get(CUSTOMERS, {"search": "ram"}).data["count"], 1
        )

    def test_phone_must_be_unique_within_a_business_only(self):
        taken = self.client.post(
            CUSTOMERS, {"name": "Dup", "phone": "9800000001"}, format="json"
        )
        two_without = [
            self.client.post(CUSTOMERS, {"name": n}, format="json").status_code
            for n in ("A", "B")
        ]

        self.assertEqual(taken.status_code, 400)
        self.assertEqual(two_without, [201, 201])
        # Another business may use the same number.
        Customer.objects.create(business=self.other_ho, name="X", phone="9822222222")
        self.assertEqual(
            self.client.post(CUSTOMERS, {"name": "Y", "phone": "9822222222"}, format="json").status_code,
            201,
        )

    def test_same_name_is_allowed(self):
        response = self.client.post(CUSTOMERS, {"name": "Ram Auto"}, format="json")

        self.assertEqual(response.status_code, 201)

    def test_delete_deactivates(self):
        sale = self.confirmed()

        response = self.client.delete(f"{CUSTOMERS}{self.customer.pk}/")

        self.assertEqual(response.status_code, 204)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.is_active)
        self.assertEqual(self.detail(sale["id"])["status"], "confirmed")

    def test_credit_to_receive_counts_confirmed_sales_less_payments(self):
        draft = self.made()
        sale = self.confirmed()
        self.pay(sale["id"], "500.00")
        self.pay(draft["id"], "200.00")

        figures = self.figures()

        self.assertEqual(figures["total_sales"], "1500.00")
        self.assertEqual(figures["credit_to_receive"], "1000.00")
        self.assertEqual(figures["advance_received"], "200.00")

    def test_cancelled_and_draft_sales_are_not_owed(self):
        self.made()
        cancelled = self.made()
        self.client.post(f"{BASE}{cancelled['id']}/cancel/", {"reason": "x"}, format="json")

        self.assertEqual(self.figures()["credit_to_receive"], "0.00")

    def test_sales_are_listed_and_filtered(self):
        paid = self.confirmed()
        self.pay(paid["id"], "1500.00")
        partial = self.confirmed()
        self.pay(partial["id"], "10.00")
        self.confirmed()
        self.made()

        self.assertEqual(self.client.get(BASE, {"payment_status": "paid"}).data["count"], 1)
        self.assertEqual(self.client.get(BASE, {"payment_status": "partial"}).data["count"], 1)
        self.assertEqual(self.client.get(BASE, {"payment_status": "unpaid"}).data["count"], 2)
        self.assertEqual(self.client.get(BASE, {"status": "draft"}).data["count"], 1)
        self.assertEqual(
            self.client.get(BASE, {"customer": self.customer.pk}).data["count"], 4
        )
        self.assertEqual(self.client.get(BASE).json()["results"][0]["sale_no"], "SAL-000004")


class SaleAccessTests(SaleTestCase):

    def staff(self, username, **perms):
        position = self.make_position(self.ho, f"P-{username}", **perms)

        return self.make_user(
            username, User.Role.STAFF, company=self.company, position=position
        )

    def test_staff_follow_their_position(self):
        sale = self.made()
        self.staff("viewer", sales=("view",))
        self.staff("seller", sales=("view", "create"))

        self.authenticate("ACME-HO", "viewer")
        self.assertEqual(self.client.get(BASE).status_code, 200)
        self.assertEqual(self.client.get(CUSTOMERS).status_code, 200)
        self.assertEqual(self.create().status_code, 403)
        self.assertEqual(self.confirm(sale["id"]).status_code, 403)
        self.assertEqual(self.pay(sale["id"], "10").status_code, 403)
        self.assertEqual(
            self.client.post(CUSTOMERS, {"name": "N"}, format="json").status_code, 403
        )

        self.authenticate("ACME-HO", "seller")
        self.assertEqual(self.create().status_code, 201)
        self.assertEqual(self.confirm(sale["id"]).status_code, 200)

    def test_staff_without_the_module_are_refused(self):
        self.staff("buyer", purchases=("view", "create"))
        self.authenticate("ACME-HO", "buyer")

        for url in (BASE, CUSTOMERS, PAYMENTS):
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_another_company_sees_nothing_and_changes_nothing(self):
        sale = self.confirmed()
        payment = self.pay(sale["id"], "10").json()

        self.authenticate("OTHER-HO", "owner")

        self.assertEqual(self.client.get(BASE).data["count"], 0)
        self.assertEqual(self.client.get(f"{BASE}{sale['id']}/").status_code, 404)
        self.assertEqual(self.confirm(sale["id"]).status_code, 404)
        self.assertEqual(self.client.get(CUSTOMERS).data["count"], 1)
        self.assertEqual(
            self.client.get(f"{CUSTOMERS}{self.customer.pk}/").status_code, 404
        )
        self.assertEqual(
            self.client.post(f"{PAYMENTS}{payment['id']}/void/", {"reason": "x"}, format="json").status_code,
            404,
        )
        self.assertEqual(self.pay(sale["id"], "5").status_code, 400)

    def test_company_owner_is_read_only_in_a_branch(self):
        self.authenticate("ACME-BR", "owner")

        self.assertEqual(self.client.get(BASE).status_code, 200)
        self.assertEqual(self.client.post(BASE, {}, format="json").status_code, 403)
        self.assertEqual(self.client.post(CUSTOMERS, {}, format="json").status_code, 403)

    def test_unauthenticated_requests_are_rejected(self):
        self.client.credentials()

        for url in (BASE, CUSTOMERS, PAYMENTS):
            self.assertEqual(self.client.get(url).status_code, 401, url)


class SaleModelTests(SaleTestCase):

    def test_database_keeps_invoice_numbers_in_line_with_status(self):
        base = dict(business=self.ho, sale_date=timezone.localdate())

        with self.assertRaises(IntegrityError), transaction.atomic():
            Sale.objects.create(number=90, status="draft", invoice_number=1, **base)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Sale.objects.create(number=91, status="confirmed", **base)

    def test_database_refuses_duplicate_numbers(self):
        sale = self.confirmed()
        saved = Sale.objects.get(pk=sale["id"])

        with self.assertRaises(IntegrityError), transaction.atomic():
            Sale.objects.create(
                business=self.ho, number=saved.number, sale_date=timezone.localdate()
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Sale.objects.create(
                business=self.ho, number=99, status="confirmed",
                invoice_number=saved.invoice_number, sale_date=timezone.localdate(),
            )
