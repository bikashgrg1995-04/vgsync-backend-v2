from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.finance.models import Expense
from apps.inventory.models import StockBalance, StockMovement
from apps.purchases.models import (
    CreditSettlement,
    Purchase,
    PurchaseReturn,
)
from apps.purchases.test_purchases import (
    BASE,
    EXPENSES,
    PAYMENTS,
    SUPPLIERS,
    PurchaseTestCase,
    dec,
)

RETURNS = "/api/v1/purchases/returns/"
CREDITS = "/api/v1/purchases/credits/"


class ReturnTestCase(PurchaseTestCase):

    def received(self, items=None, **header):
        purchase = self.made(items=items, **header)
        response = self.receive(purchase["id"])
        self.assertEqual(response.status_code, 200, response.data)

        return response.json()

    def item_id(self, purchase, index=0):
        return purchase["items"][index]["id"]

    def send_back(self, purchase, quantity="4", index=0, **extra):
        """Return ``quantity`` of one line of ``purchase``."""
        line = {
            "purchase_item": self.item_id(purchase, index),
            "quantity": quantity,
            **extra.pop("line", {}),
        }
        payload = {
            "purchase": purchase["id"],
            "reason": "Wrong specification",
            "items": [line],
            **extra,
        }

        return self.client.post(RETURNS, payload, format="json")

    def returned(self, purchase, quantity="4", **extra):
        response = self.send_back(purchase, quantity, **extra)
        self.assertEqual(response.status_code, 201, response.data)

        return response.json()

    def credit(self, supplier=None):
        data = self.client.get(f"{SUPPLIERS}{(supplier or self.supplier).pk}/").json()

        return data["credit_to_receive"]

    def refund(self, amount, **extra):
        return self.client.post(
            CREDITS,
            {"supplier": self.supplier.pk, "kind": "refund", "amount": amount,
             "method": "bank", **extra},
            format="json",
        )

    def apply(self, purchase_id, amount, **extra):
        return self.client.post(
            CREDITS,
            {"supplier": self.supplier.pk, "kind": "adjustment", "amount": amount,
             "purchase": purchase_id, **extra},
            format="json",
        )

    def detail(self, purchase_id):
        return self.client.get(f"{BASE}{purchase_id}/").json()


class ReturnTests(ReturnTestCase):

    def test_a_return_takes_goods_out_of_stock_and_creates_credit(self):
        purchase = self.received()

        data = self.returned(purchase, "4")

        self.assertEqual(data["return_no"], "PRT-000001")
        self.assertEqual(data["status"], "active")
        self.assertEqual(data["credit_amount"], "400.00")
        self.assertEqual(self.stock(), dec("6"))

        movement = StockMovement.objects.get(movement_type="purchase_return")
        self.assertEqual(movement.quantity, dec("4"))
        self.assertEqual(movement.reference, "PRT-000001")
        self.assertEqual(movement.balance_before, dec("10"))
        self.assertEqual(movement.balance_after, dec("6"))
        self.assertEqual(movement.created_by, self.owner)

    def test_credit_includes_the_share_of_discount_and_vat_but_not_freight(self):
        purchase = self.received(
            discount_amount="100.00", vat_rate="13", freight_amount="200.00"
        )

        # 1000 - 100 = 900; VAT 117; freight 200 -> net 1217
        self.assertEqual(purchase["net_total"], "1217.00")

        part = self.returned(purchase, "4")

        # 400 - 40 (share of discount) = 360; + 13% = 46.80
        self.assertEqual(part["credit_amount"], "406.80")

        rest = self.returned(purchase, "6")

        self.assertEqual(rest["credit_amount"], "610.20")
        # Everything returned: credit is the goods with discount and VAT.
        self.assertEqual(self.credit(), "1017.00")

    def test_credits_can_never_add_up_to_more_than_the_goods_were_worth(self):
        purchase = self.received(
            items=[self.line(quantity="3", price="33.33")],
            discount_amount="0.01", vat_rate="13",
        )
        total = Decimal("0")

        for _ in range(3):
            total += dec(self.returned(purchase, "1")["credit_amount"])

        # 99.99 - 0.01 = 99.98; VAT 13.00 (rounded) -> 112.98
        self.assertLessEqual(total, dec(purchase["net_total"]))
        self.assertEqual(self.credit(), f"{total:.2f}")

    def test_a_line_can_be_returned_in_parts_until_nothing_is_left(self):
        purchase = self.received()

        self.returned(purchase, "4")
        item = self.detail(purchase["id"])["items"][0]
        self.assertEqual(item["returned_quantity"], "4.000")
        self.assertEqual(item["returnable_quantity"], "6.000")

        too_many = self.send_back(purchase, "6.001")
        self.assertEqual(too_many.status_code, 400)
        self.assertIn("only 6", str(too_many.data["items"]))

        self.returned(purchase, "6")
        again = self.send_back(purchase, "0.001")

        self.assertEqual(again.status_code, 400)
        self.assertEqual(self.stock(), 0)

    def test_a_line_of_another_purchase_is_refused(self):
        first = self.received()
        second = self.received()

        response = self.client.post(
            RETURNS,
            {
                "purchase": first["id"],
                "reason": "x",
                "items": [{"purchase_item": self.item_id(second), "quantity": "1"}],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stock(), dec("20"))
        self.assertFalse(PurchaseReturn.objects.exists())

    def test_a_line_listed_twice_is_refused(self):
        purchase = self.received()
        item = self.item_id(purchase)

        response = self.client.post(
            RETURNS,
            {
                "purchase": purchase["id"],
                "reason": "x",
                "items": [
                    {"purchase_item": item, "quantity": "6"},
                    {"purchase_item": item, "quantity": "6"},
                ],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stock(), dec("10"))

    def test_only_received_purchases_can_be_returned(self):
        draft = self.made()
        cancelled = self.made()
        self.client.post(f"{BASE}{cancelled['id']}/cancel/", {"reason": "x"}, format="json")

        for purchase in (draft, cancelled):
            response = self.send_back(purchase, "1")

            self.assertEqual(response.status_code, 409, purchase["status"])
            self.assertEqual(response.data["code"], "invalid_status")

    def test_goods_already_sold_cannot_be_returned(self):
        purchase = self.received()
        StockBalance.objects.filter(product=self.oil).update(quantity=dec("2"))

        response = self.send_back(purchase, "4")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PurchaseReturn.objects.exists())
        self.assertEqual(self.credit(), "0.00")
        self.assertEqual(self.stock(), dec("2"))

    def test_if_one_line_fails_nothing_is_returned(self):
        purchase = self.received(items=[self.line(self.oil), self.line(self.filter)])
        StockBalance.objects.filter(product=self.filter).update(quantity=dec("1"))

        response = self.client.post(
            RETURNS,
            {
                "purchase": purchase["id"],
                "reason": "x",
                "items": [
                    {"purchase_item": self.item_id(purchase, 0), "quantity": "5"},
                    {"purchase_item": self.item_id(purchase, 1), "quantity": "5"},
                ],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stock(self.oil), dec("10"))
        self.assertFalse(PurchaseReturn.objects.exists())
        self.assertEqual(StockMovement.objects.filter(movement_type="purchase_return").count(), 0)

    def test_goods_can_be_taken_from_damaged_stock(self):
        purchase = self.received()

        refused = self.send_back(purchase, "2", line={"stock_condition": "damaged"})
        StockBalance.objects.filter(product=self.oil).update(damaged_quantity=dec("3"))
        accepted = self.send_back(purchase, "2", line={"stock_condition": "damaged"})

        self.assertEqual(refused.status_code, 400)
        self.assertEqual(accepted.status_code, 201, accepted.data)
        balance = StockBalance.objects.get(product=self.oil)
        self.assertEqual(balance.damaged_quantity, dec("1"))
        self.assertEqual(balance.quantity, dec("10"))

    def test_input_rules(self):
        purchase = self.received()

        for bad in (
            {"reason": ""},
            {"reason": "   "},
            {"items": []},
            {"return_date": "2999-01-01"},
        ):
            self.assertEqual(self.send_back(purchase, **bad).status_code, 400, bad)

        self.assertEqual(self.send_back(purchase, "0").status_code, 400)
        self.assertEqual(self.send_back(purchase, "-1").status_code, 400)
        self.assertEqual(
            self.send_back(purchase, "1", line={"stock_condition": "scrap"}).status_code, 400
        )
        self.assertEqual(self.stock(), dec("10"))

    def test_numbers_run_on_within_a_business(self):
        purchase = self.received()

        first = self.returned(purchase, "1")
        second = self.returned(purchase, "1")

        self.assertEqual((first["number"], second["number"]), (1, 2))

    def test_returns_are_listed_and_filtered(self):
        one = self.received()
        two = self.received()
        self.returned(one, "1")
        self.returned(two, "2")

        by_purchase = self.client.get(RETURNS, {"purchase": two["id"]})
        everything = self.client.get(RETURNS)

        self.assertEqual(by_purchase.data["count"], 1)
        self.assertEqual(by_purchase.json()["results"][0]["credit_amount"], "200.00")
        self.assertEqual(everything.json()["results"][0]["return_no"], "PRT-000002")

    def test_returns_cannot_be_edited_or_deleted(self):
        purchase = self.received()
        ret = self.returned(purchase, "1")

        self.assertEqual(
            self.client.patch(f"{RETURNS}{ret['id']}/", {"reason": "x"}, format="json").status_code,
            405,
        )
        self.assertEqual(self.client.delete(f"{RETURNS}{ret['id']}/").status_code, 405)


class CreditKeptApartTests(ReturnTestCase):

    def test_credit_is_shown_separately_and_owed_money_does_not_change(self):
        purchase = self.received()

        before = self.client.get(f"{SUPPLIERS}{self.supplier.pk}/").json()
        self.returned(purchase, "4")
        after = self.client.get(f"{SUPPLIERS}{self.supplier.pk}/").json()

        self.assertEqual(before["credit_to_receive"], "0.00")
        self.assertEqual(after["credit_to_receive"], "400.00")
        self.assertEqual(after["outstanding"], before["outstanding"])
        self.assertEqual(after["total_purchased"], "1000.00")

        detail = self.detail(purchase["id"])
        self.assertEqual(detail["remaining_amount"], "1000.00")
        self.assertEqual(detail["payment_status"], "unpaid")

    def test_a_return_does_not_touch_expenses(self):
        purchase = self.received()
        self.pay(purchase["id"], "1000.00")

        self.returned(purchase, "4")

        summary = self.client.get(f"{EXPENSES}summary/").json()
        self.assertEqual(summary["total"], "1000.00")
        self.assertEqual(summary["purchase_refunds"], "0.00")
        self.assertEqual(summary["net_total"], "1000.00")

    def test_a_refund_uses_up_credit_and_reduces_net_spending(self):
        purchase = self.received()
        self.pay(purchase["id"], "1000.00")
        self.returned(purchase, "4")

        response = self.refund("400.00", reference="TXN-5")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.json()["kind"], "refund")
        self.assertEqual(self.credit(), "0.00")

        summary = self.client.get(f"{EXPENSES}summary/").json()
        self.assertEqual(summary["total"], "1000.00")
        self.assertEqual(summary["purchase_refunds"], "400.00")
        self.assertEqual(summary["net_total"], "600.00")
        # The original expense stays as it was.
        self.assertEqual(Expense.objects.get().amount, dec("1000"))

    def test_refunds_follow_the_summary_filters(self):
        purchase = self.received()
        self.pay(purchase["id"], "1000.00")
        self.returned(purchase, "4")
        self.refund("100.00")

        later = self.client.get(f"{EXPENSES}summary/", {"expense_date__gte": "2999-01-01"})
        other_category = self.client.get(f"{EXPENSES}summary/", {"category": "rent"})

        self.assertEqual(later.json()["purchase_refunds"], "0.00")
        self.assertEqual(other_category.json()["purchase_refunds"], "0.00")

    def test_refund_rules(self):
        purchase = self.received()
        self.returned(purchase, "4")

        self.assertEqual(self.refund("400.01").status_code, 400)
        self.assertEqual(self.refund("0").status_code, 400)
        self.assertEqual(self.refund("10", method="").status_code, 400)
        self.assertEqual(self.refund("10", method="barter").status_code, 400)
        self.assertEqual(self.refund("10", purchase=purchase["id"]).status_code, 400)
        self.assertEqual(self.refund("10", settled_on="2999-01-01").status_code, 400)
        self.assertEqual(self.credit(), "400.00")

        self.assertEqual(self.refund("150.00").status_code, 201)
        self.assertEqual(self.refund("250.00").status_code, 201)
        self.assertEqual(self.refund("0.01").status_code, 400)

    def test_credit_can_be_applied_to_a_purchase(self):
        old = self.received()
        self.returned(old, "4")
        replacement = self.received(items=[self.line(quantity="3", price="100.00")])

        response = self.apply(replacement["id"], "300.00")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.json()["purchase_no"], replacement["purchase_no"])
        self.assertEqual(self.credit(), "100.00")

        detail = self.detail(replacement["id"])
        self.assertEqual(detail["credit_applied"], "300.00")
        self.assertEqual(detail["remaining_amount"], "0.00")
        self.assertEqual(detail["payment_status"], "paid")
        self.assertEqual(detail["paid_amount"], "0.00")
        # No cash moved, so no expense and no refund.
        self.assertFalse(Expense.objects.exists())

    def test_replacement_goods_are_paid_for_by_credit(self):
        original = self.received()
        self.returned(original, "4")

        replacement = self.made(items=[self.line(quantity="4", price="100.00")])
        self.apply(replacement["id"], "400.00")
        self.receive(replacement["id"])

        self.assertEqual(self.stock(), dec("10"))
        self.assertEqual(self.credit(), "0.00")
        supplier = self.client.get(f"{SUPPLIERS}{self.supplier.pk}/").json()
        # Only the original 1000 is still owed.
        self.assertEqual(supplier["outstanding"], "1000.00")

    def test_credit_applied_cannot_exceed_the_credit_or_what_the_purchase_owes(self):
        old = self.received()
        self.returned(old, "4")
        small = self.received(items=[self.line(quantity="1", price="100.00")])

        self.assertEqual(self.apply(small["id"], "100.01").status_code, 400)
        self.assertEqual(self.apply(old["id"], "400.01").status_code, 400)

        big = self.received(items=[self.line(quantity="10", price="100.00")])
        self.assertEqual(self.apply(big["id"], "400.01").status_code, 400)
        self.assertEqual(self.apply(big["id"], "400.00").status_code, 201)

    def test_credit_applied_rules(self):
        old = self.received()
        self.returned(old, "4")
        other = Purchase.objects.get(pk=self.received()["id"])
        cancelled = self.made()
        self.client.post(f"{BASE}{cancelled['id']}/cancel/", {"reason": "x"}, format="json")

        self.assertEqual(
            self.client.post(
                CREDITS, {"supplier": self.supplier.pk, "kind": "adjustment", "amount": "10"},
                format="json",
            ).status_code,
            400,
        )
        self.assertEqual(self.apply(old["id"], "10", method="cash").status_code, 400)
        self.assertEqual(self.apply(cancelled["id"], "10").status_code, 409)
        self.assertEqual(other.status, "received")

    def test_credit_of_another_supplier_cannot_be_used(self):
        purchase = self.received()
        self.returned(purchase, "4")
        second = Purchase.objects.create(
            business=self.ho, number=99, supplier=self.supplier,
            purchase_date=timezone.localdate(),
        )
        from apps.purchases.models import Supplier

        other = Supplier.objects.create(business=self.ho, name="Other Supplier")
        foreign = self.client.post(
            CREDITS,
            {"supplier": other.pk, "kind": "adjustment", "amount": "10",
             "purchase": purchase["id"]},
            format="json",
        )

        self.assertEqual(foreign.status_code, 400)
        self.assertEqual(second.status, "draft")

    def test_paying_cannot_exceed_what_credit_has_not_covered(self):
        purchase = self.received()
        self.returned(purchase, "4")
        self.apply(purchase["id"], "400.00")

        too_much = self.pay(purchase["id"], "600.01")
        exact = self.pay(purchase["id"], "600.00")

        self.assertEqual(too_much.status_code, 400)
        self.assertEqual(exact.status_code, 201)
        self.assertEqual(self.detail(purchase["id"])["payment_status"], "paid")

    def test_voiding_applied_credit_restores_credit_and_what_is_owed(self):
        purchase = self.received()
        self.returned(purchase, "4")
        settlement = self.apply(purchase["id"], "400.00").json()

        response = self.client.post(
            f"{CREDITS}{settlement['id']}/void/", {"reason": "wrong bill"}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.credit(), "400.00")
        self.assertEqual(self.detail(purchase["id"])["remaining_amount"], "1000.00")

        again = self.client.post(
            f"{CREDITS}{settlement['id']}/void/", {"reason": "x"}, format="json"
        )
        self.assertEqual(again.status_code, 409)

    def test_a_draft_with_applied_credit_cannot_be_cancelled_or_cut_below_it(self):
        old = self.received()
        self.returned(old, "4")
        draft = self.made(items=[self.line(quantity="3", price="100.00")])
        self.apply(draft["id"], "300.00")

        cancel = self.client.post(
            f"{BASE}{draft['id']}/cancel/", {"reason": "x"}, format="json"
        )
        cut = self.client.patch(
            f"{BASE}{draft['id']}/", {"discount_amount": "50.00"}, format="json"
        )

        self.assertEqual(cancel.status_code, 409)
        self.assertEqual(cut.status_code, 400)

    def test_filters_on_the_purchase_list_count_credit(self):
        purchase = self.received()
        self.returned(purchase, "4")
        self.apply(purchase["id"], "400.00")

        partial = self.client.get(BASE, {"payment_status": "partial"})
        unpaid = self.client.get(BASE, {"payment_status": "unpaid"})

        self.assertEqual(partial.data["count"], 1)
        self.assertEqual(unpaid.data["count"], 0)

    def test_settlements_are_listed_and_filtered(self):
        purchase = self.received()
        self.returned(purchase, "4")
        self.refund("100.00")
        self.apply(purchase["id"], "200.00")

        refunds = self.client.get(CREDITS, {"kind": "refund"})
        everything = self.client.get(CREDITS)

        self.assertEqual(refunds.data["count"], 1)
        self.assertEqual(everything.data["count"], 2)

    def test_the_supplier_outstanding_counts_applied_credit(self):
        purchase = self.received()
        self.returned(purchase, "4")
        self.apply(purchase["id"], "300.00")

        supplier = self.client.get(f"{SUPPLIERS}{self.supplier.pk}/").json()

        self.assertEqual(supplier["outstanding"], "700.00")
        self.assertEqual(supplier["credit_to_receive"], "100.00")


class VoidReturnTests(ReturnTestCase):

    def test_voiding_a_return_puts_goods_back_and_removes_the_credit(self):
        purchase = self.received()
        ret = self.returned(purchase, "4")

        response = self.client.post(
            f"{RETURNS}{ret['id']}/void/", {"reason": "Supplier refused"}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["status"], "void")
        self.assertEqual(self.stock(), dec("10"))
        self.assertEqual(self.credit(), "0.00")
        self.assertEqual(
            self.detail(purchase["id"])["items"][0]["returnable_quantity"], "10.000"
        )

        movement = StockMovement.objects.filter(reference="PRT-000001").first()
        self.assertEqual(movement.movement_type, "adjustment_in")
        # The history stays: the return itself is still there.
        self.assertEqual(self.client.get(f"{RETURNS}{ret['id']}/").status_code, 200)

        # The same goods can be returned again.
        self.assertEqual(self.send_back(purchase, "10").status_code, 201)

    def test_a_return_whose_credit_was_used_cannot_be_voided(self):
        purchase = self.received()
        ret = self.returned(purchase, "4")
        settlement = self.refund("300.00").json()

        blocked = self.client.post(
            f"{RETURNS}{ret['id']}/void/", {"reason": "x"}, format="json"
        )
        self.client.post(f"{CREDITS}{settlement['id']}/void/", {"reason": "x"}, format="json")
        allowed = self.client.post(
            f"{RETURNS}{ret['id']}/void/", {"reason": "x"}, format="json"
        )

        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.data["code"], "credit_in_use")
        self.assertEqual(allowed.status_code, 200)

    def test_a_return_is_voided_once_and_needs_a_reason(self):
        purchase = self.received()
        ret = self.returned(purchase, "4")

        self.assertEqual(
            self.client.post(f"{RETURNS}{ret['id']}/void/", {}, format="json").status_code, 400
        )
        self.client.post(f"{RETURNS}{ret['id']}/void/", {"reason": "x"}, format="json")
        again = self.client.post(f"{RETURNS}{ret['id']}/void/", {"reason": "x"}, format="json")

        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.stock(), dec("10"))


class ReturnAccessTests(ReturnTestCase):

    def staff(self, username, **perms):
        position = self.make_position(self.ho, f"P-{username}", **perms)

        # Sending goods back is its own permission (``purchases.return``).
        if "create" in perms.get("purchases", ()):
            position.permissions.filter(module="purchases").update(
                actions=["return"]
            )

        return self.make_user(
            username, User.Role.STAFF, company=self.company, position=position
        )

    def test_staff_follow_their_position(self):
        purchase = self.received()
        ret = self.returned(purchase, "4")
        self.staff("viewer", purchases=("view",))
        self.staff("buyer", purchases=("view", "create"))

        self.authenticate("ACME-HO", "viewer")
        self.assertEqual(self.client.get(RETURNS).status_code, 200)
        self.assertEqual(self.client.get(CREDITS).status_code, 200)
        self.assertEqual(self.send_back(purchase, "1").status_code, 403)
        self.assertEqual(self.refund("10").status_code, 403)
        self.assertEqual(
            self.client.post(f"{RETURNS}{ret['id']}/void/", {"reason": "x"}, format="json").status_code,
            403,
        )

        self.authenticate("ACME-HO", "buyer")
        self.assertEqual(self.send_back(purchase, "1").status_code, 201)

    def test_staff_without_the_module_are_refused(self):
        self.staff("sales", sales=("view",))
        self.authenticate("ACME-HO", "sales")

        for url in (RETURNS, CREDITS):
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_another_company_sees_nothing_and_changes_nothing(self):
        purchase = self.received()
        ret = self.returned(purchase, "4")

        self.authenticate("OTHER-HO", "owner")

        self.assertEqual(self.client.get(RETURNS).data["count"], 0)
        self.assertEqual(self.client.get(f"{RETURNS}{ret['id']}/").status_code, 404)
        self.assertEqual(
            self.client.post(f"{RETURNS}{ret['id']}/void/", {"reason": "x"}, format="json").status_code,
            404,
        )
        self.assertEqual(self.send_back(purchase, "1").status_code, 400)
        self.assertEqual(self.refund("1").status_code, 400)
        self.assertEqual(self.client.get(CREDITS).data["count"], 0)

    def test_company_owner_is_read_only_in_a_branch(self):
        self.authenticate("ACME-BR", "owner")

        self.assertEqual(self.client.get(RETURNS).status_code, 200)
        self.assertEqual(self.client.post(RETURNS, {}, format="json").status_code, 403)
        self.assertEqual(self.client.post(CREDITS, {}, format="json").status_code, 403)

    def test_unauthenticated_requests_are_rejected(self):
        self.client.credentials()

        self.assertEqual(self.client.get(RETURNS).status_code, 401)
        self.assertEqual(self.client.get(CREDITS).status_code, 401)


class ReturnModelTests(ReturnTestCase):

    def test_database_keeps_the_settlement_in_line_with_its_kind(self):
        purchase = Purchase.objects.create(
            business=self.ho, number=1, supplier=self.supplier,
            purchase_date=timezone.localdate(),
        )
        base = dict(
            supplier=self.supplier, amount=Decimal("10"),
            settled_on=timezone.localdate(),
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            CreditSettlement.objects.create(kind="adjustment", **base)

        with self.assertRaises(IntegrityError), transaction.atomic():
            CreditSettlement.objects.create(kind="refund", purchase=purchase, **base)

        with self.assertRaises(IntegrityError), transaction.atomic():
            CreditSettlement.objects.create(kind="refund", **{**base, "amount": Decimal("0")})

    def test_database_refuses_a_duplicate_return_number(self):
        purchase = self.received()
        self.returned(purchase, "1")
        saved = PurchaseReturn.objects.get()

        with self.assertRaises(IntegrityError), transaction.atomic():
            PurchaseReturn.objects.create(
                business=self.ho, number=saved.number, purchase_id=purchase["id"],
                supplier=self.supplier, return_date=timezone.localdate(), reason="x",
            )


class CreditToPayTests(ReturnTestCase):

    def figures(self):
        return self.client.get(f"{SUPPLIERS}{self.supplier.pk}/").json()

    def test_an_order_is_credit_to_pay_before_and_after_the_goods_arrive(self):
        purchase = self.made()

        ordered = self.figures()
        self.assertEqual(ordered["credit_to_pay"], "1000.00")
        self.assertEqual(ordered["to_pay_on_order"], "1000.00")
        self.assertEqual(ordered["outstanding"], "0.00")

        self.pay(purchase["id"], "400.00")
        advance = self.figures()
        self.assertEqual(advance["credit_to_pay"], "600.00")
        self.assertEqual(advance["advance_paid"], "400.00")

        self.receive(purchase["id"])
        received = self.figures()
        self.assertEqual(received["to_pay_on_order"], "0.00")
        self.assertEqual(received["outstanding"], "600.00")
        self.assertEqual(received["credit_to_pay"], "600.00")

        self.pay(purchase["id"], "600.00")
        self.assertEqual(self.figures()["credit_to_pay"], "0.00")

    def test_cancelled_orders_are_not_owed(self):
        purchase = self.made()
        self.client.post(f"{BASE}{purchase['id']}/cancel/", {"reason": "x"}, format="json")

        self.assertEqual(self.figures()["credit_to_pay"], "0.00")

    def test_a_return_does_not_change_credit_to_pay(self):
        purchase = self.received()
        self.returned(purchase, "4")

        figures = self.figures()

        self.assertEqual(figures["credit_to_pay"], "1000.00")
        self.assertEqual(figures["credit_to_receive"], "400.00")


class ReplacementTests(ReturnTestCase):

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

        return self.client.post(CREDITS, payload, format="json")

    def figures(self):
        return self.client.get(f"{SUPPLIERS}{self.supplier.pk}/").json()

    def setUp(self):
        super().setUp()
        self.purchase = self.received()
        self.returned(self.purchase, "4")

    def test_replacement_uses_credit_and_becomes_stock_to_receive(self):
        response = self.replace()

        self.assertEqual(response.status_code, 201, response.data)
        data = response.json()
        self.assertEqual(data["kind"], "replacement")
        self.assertEqual(data["delivery_status"], "pending")
        self.assertEqual(data["items"][0]["quantity"], "4.000")
        self.assertIsNone(data["received_on"])

        figures = self.figures()
        self.assertEqual(figures["credit_to_receive"], "0.00")
        self.assertEqual(figures["stock_to_receive"], "4.000")
        # Nothing has arrived and no cash moved.
        self.assertEqual(self.stock(), dec("6"))
        summary = self.client.get(f"{EXPENSES}summary/").json()
        self.assertEqual(summary["purchase_refunds"], "0.00")
        self.assertEqual(figures["credit_to_pay"], "1000.00")

    def test_receiving_the_goods_adds_them_to_stock_once(self):
        settlement = self.replace().json()

        response = self.client.post(f"{CREDITS}{settlement['id']}/receive/", {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["delivery_status"], "received")
        self.assertEqual(self.stock(), dec("10"))
        self.assertEqual(self.figures()["stock_to_receive"], "0.000")
        movement = StockMovement.objects.filter(reference__startswith="RPL-").get()
        self.assertEqual(movement.movement_type, "purchase_receipt")
        self.assertEqual(movement.quantity, dec("4"))

        again = self.client.post(f"{CREDITS}{settlement['id']}/receive/", {}, format="json")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.data["code"], "already_received")
        self.assertEqual(self.stock(), dec("10"))

    def test_different_goods_can_be_sent_instead(self):
        settlement = self.replace(
            items=[{"product": self.filter.pk, "quantity": "2"}]
        ).json()

        self.client.post(f"{CREDITS}{settlement['id']}/receive/", {}, format="json")

        self.assertEqual(self.stock(self.filter), dec("2"))
        self.assertEqual(self.stock(), dec("6"))

    def test_a_pending_replacement_can_be_voided_to_get_the_credit_back(self):
        settlement = self.replace().json()

        response = self.client.post(
            f"{CREDITS}{settlement['id']}/void/", {"reason": "Supplier cancelled"}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        figures = self.figures()
        self.assertEqual(figures["credit_to_receive"], "400.00")
        self.assertEqual(figures["stock_to_receive"], "0.000")

        late = self.client.post(f"{CREDITS}{settlement['id']}/receive/", {}, format="json")
        self.assertEqual(late.status_code, 409)
        self.assertEqual(self.stock(), dec("6"))

    def test_a_received_replacement_cannot_be_voided(self):
        settlement = self.replace().json()
        self.client.post(f"{CREDITS}{settlement['id']}/receive/", {}, format="json")

        response = self.client.post(
            f"{CREDITS}{settlement['id']}/void/", {"reason": "x"}, format="json"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "already_received")
        self.assertEqual(self.credit(), "0.00")

    def test_replacement_rules(self):
        product = self.oil.pk

        self.assertEqual(self.replace(amount="400.01").status_code, 400)
        self.assertEqual(self.replace(items=[]).status_code, 400)
        self.assertEqual(
            self.replace(items=[{"product": product, "quantity": "0"}]).status_code, 400
        )
        self.assertEqual(
            self.replace(items=[
                {"product": product, "quantity": "1"},
                {"product": product, "quantity": "1"},
            ]).status_code,
            400,
        )
        self.assertEqual(self.replace(method="cash").status_code, 400)
        self.assertEqual(self.replace(purchase=self.purchase["id"]).status_code, 400)
        self.assertEqual(self.refund("10", items=[{"product": product, "quantity": "1"}]).status_code, 400)
        self.assertEqual(self.credit(), "400.00")

    def test_only_a_replacement_can_be_received(self):
        refund = self.refund("100.00").json()

        response = self.client.post(f"{CREDITS}{refund['id']}/receive/", {}, format="json")

        self.assertEqual(response.status_code, 409)

    def test_receive_date_cannot_be_in_the_future(self):
        settlement = self.replace().json()

        response = self.client.post(
            f"{CREDITS}{settlement['id']}/receive/", {"received_on": "2999-01-01"}, format="json"
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stock(), dec("6"))

    def test_pending_replacements_can_be_listed(self):
        first = self.replace("100.00", items=[{"product": self.oil.pk, "quantity": "1"}]).json()
        self.replace("100.00", items=[{"product": self.oil.pk, "quantity": "1"}])
        self.client.post(f"{CREDITS}{first['id']}/receive/", {}, format="json")

        pending = self.client.get(CREDITS, {"kind": "replacement", "received_on__isnull": "true"})

        self.assertEqual(pending.data["count"], 1)

    def test_receiving_needs_the_create_permission(self):
        settlement = self.replace().json()
        position = self.make_position(self.ho, "P-viewer", purchases=("view",))
        self.make_user("viewer", User.Role.STAFF, company=self.company, position=position)

        self.authenticate("ACME-HO", "viewer")
        response = self.client.post(f"{CREDITS}{settlement['id']}/receive/", {}, format="json")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get(CREDITS).status_code, 200)
