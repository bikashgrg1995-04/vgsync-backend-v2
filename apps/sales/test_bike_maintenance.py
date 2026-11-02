from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.finance.models import Income
from apps.inventory.models import Product, StockBalance, StockMovement
from apps.inventory.services.stock import create_stock_movement
from apps.sales.models import BikeUnit, EmiInstallment, Followup, Sale
from apps.sales.test_sales import (
    BASE,
    CUSTOMERS,
    PAYMENTS,
    SaleTestCase,
    dec,
)

BIKES = "/api/v1/sales/bikes/"
FOLLOWUPS = "/api/v1/sales/followups/"
INCOME = "/api/v1/finance/income/"


class BikeTestCase(SaleTestCase):

    def setUp(self):
        super().setUp()

        self.model = Product.objects.create(
            owner_business=self.ho, stock_no="BIKE-1", name="Pulsar 150",
            sale_price=Decimal("100000.00"),
        )
        create_stock_movement(
            user=self.owner, business=self.ho, product=self.model,
            movement_type=StockMovement.MovementType.OPENING, quantity="5",
        )

    # -- helpers -----------------------------------------------------------

    def unit(self, chassis="CH-001", engine="EN-001", **extra):
        response = self.client.post(
            BIKES,
            {"product": self.model.pk, "chassis_no": chassis, "engine_no": engine,
             "color": "Black", **extra},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)

        return response.json()

    def bike_sale(self, units=None, **extra):
        units = units or [self.unit()]
        payload = {
            "sale_type": "bike",
            "customer": self.customer.pk,
            "items": [{"bike_unit": u["id"]} for u in units],
            **extra,
        }

        return self.client.post(BASE, payload, format="json")

    def made_bike(self, units=None, **extra):
        response = self.bike_sale(units, **extra)
        self.assertEqual(response.status_code, 201, response.data)

        return response.json()

    def emi_terms(self, **extra):
        return {
            "payment_mode": "emi", "down_payment": "20000.00",
            "emi_months": 10, "emi_amount": "9000.00", **extra,
        }

    def followups(self, **params):
        return self.client.get(FOLLOWUPS, params).json()["results"]

    def stock_of(self, product):
        return StockBalance.objects.get(product=product, business=self.ho).quantity


class BikeUnitTests(BikeTestCase):

    def test_a_unit_is_registered_against_the_stock_of_its_model(self):
        data = self.unit()

        self.assertEqual(data["status"], "in_stock")
        self.assertEqual(data["product_name"], "Pulsar 150")
        # Registering a unit does not change stock.
        self.assertEqual(self.stock_of(self.model), dec("5"))

    def test_units_cannot_outnumber_the_stock(self):
        for number in range(5):
            self.unit(chassis=f"CH-{number}", engine=f"EN-{number}")

        response = self.client.post(
            BIKES, {"product": self.model.pk, "chassis_no": "CH-X"}, format="json"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "not_in_stock")

    def test_chassis_and_engine_numbers_are_unique_everywhere(self):
        self.unit()

        same_chassis = self.client.post(
            BIKES, {"product": self.model.pk, "chassis_no": "ch-001"}, format="json"
        )
        same_engine = self.client.post(
            BIKES,
            {"product": self.model.pk, "chassis_no": "CH-002", "engine_no": "en-001"},
            format="json",
        )

        self.assertEqual(same_chassis.status_code, 400)
        self.assertEqual(same_engine.status_code, 400)

        # Another business cannot register the same chassis either.
        with self.assertRaises(IntegrityError), transaction.atomic():
            BikeUnit.objects.create(
                business=self.other_ho, product=self.other_product, chassis_no="CH-001"
            )

    def test_an_engine_number_is_optional(self):
        self.unit(chassis="CH-A", engine="")
        self.unit(chassis="CH-B", engine="")

    def test_a_unit_can_be_edited_until_it_is_sold(self):
        data = self.unit()

        edited = self.client.patch(
            f"{BIKES}{data['id']}/", {"color": "Red"}, format="json"
        )
        self.assertEqual(edited.json()["color"], "Red")

        sale = self.made_bike(units=[data])
        self.confirm(sale["id"])

        locked = self.client.patch(
            f"{BIKES}{data['id']}/", {"color": "Blue"}, format="json"
        )
        self.assertEqual(locked.status_code, 400)

    def test_the_model_of_a_unit_cannot_be_changed(self):
        data = self.unit()

        response = self.client.patch(
            BIKES + f"{data['id']}/", {"product": self.oil.pk}, format="json"
        )

        self.assertEqual(response.status_code, 400)

    def test_units_are_listed_filtered_and_searched(self):
        self.unit(chassis="AAA", engine="E1")
        sold = self.unit(chassis="BBB", engine="E2")
        self.confirm(self.made_bike(units=[sold])["id"])

        self.assertEqual(self.client.get(BIKES).data["count"], 2)
        self.assertEqual(self.client.get(BIKES, {"status": "in_stock"}).data["count"], 1)
        self.assertEqual(self.client.get(BIKES, {"search": "bbb"}).data["count"], 1)

    def test_units_belong_to_one_business(self):
        data = self.unit()

        self.authenticate("OTHER-HO", "owner")

        self.assertEqual(self.client.get(BIKES).data["count"], 0)
        self.assertEqual(self.client.get(f"{BIKES}{data['id']}/").status_code, 404)

    def test_units_cannot_be_deleted(self):
        data = self.unit()

        self.assertEqual(self.client.delete(f"{BIKES}{data['id']}/").status_code, 405)


class BikeSaleTests(BikeTestCase):

    def test_a_bike_sale_sells_a_unit_at_the_model_price(self):
        unit = self.unit()

        sale = self.made_bike(units=[unit])

        self.assertEqual(sale["sale_type"], "bike")
        self.assertEqual(sale["payment_mode"], "full")
        self.assertEqual(sale["net_total"], "100000.00")
        self.assertEqual(sale["items"][0]["chassis_no"], "CH-001")
        self.assertEqual(sale["items"][0]["quantity"], "1.000")
        self.assertEqual(sale["amount_due"], "100000.00")

    def test_confirming_takes_one_from_stock_and_marks_the_unit_sold(self):
        unit = self.unit()
        sale = self.made_bike(units=[unit])

        self.confirm(sale["id"])

        self.assertEqual(self.stock_of(self.model), dec("4"))
        self.assertEqual(BikeUnit.objects.get(pk=unit["id"]).status, "sold")
        movement = StockMovement.objects.get(movement_type="sale_issue")
        self.assertIn("CH-001", movement.notes)

    def test_the_same_bike_cannot_be_sold_twice(self):
        unit = self.unit()
        first = self.made_bike(units=[unit])
        second = self.made_bike(units=[unit])
        self.confirm(first["id"])

        refused = self.confirm(second["id"])
        new = self.bike_sale(units=[unit])

        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.data["code"], "bike_unavailable")
        self.assertEqual(new.status_code, 400)
        self.assertEqual(self.stock_of(self.model), dec("4"))

    def test_a_failed_confirm_leaves_the_unit_in_stock(self):
        unit = self.unit()
        sale = self.made_bike(units=[unit], customer=None)

        refused = self.confirm(sale["id"])

        self.assertEqual(refused.status_code, 409)
        self.assertEqual(BikeUnit.objects.get(pk=unit["id"]).status, "in_stock")

    def test_bike_sale_line_rules(self):
        unit = self.unit()

        for items in (
            [{"product": self.oil.pk, "quantity": "1"}],
            [{"bike_unit": unit["id"], "product": self.model.pk}],
            [{"bike_unit": unit["id"], "quantity": "2"}],
            [{"bike_unit": unit["id"]}, {"bike_unit": unit["id"]}],
            [{"description": "Service", "unit_price": "10"}],
        ):
            response = self.client.post(
                BASE, {"sale_type": "bike", "items": items}, format="json"
            )

            self.assertEqual(response.status_code, 400, items)

    def test_a_unit_of_another_business_cannot_be_sold(self):
        foreign = BikeUnit.objects.create(
            business=self.other_ho, product=self.other_product, chassis_no="F-1"
        )

        response = self.client.post(
            BASE, {"sale_type": "bike", "items": [{"bike_unit": foreign.pk}]},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_bike_units_cannot_go_on_a_stock_sale(self):
        unit = self.unit()

        response = self.client.post(
            BASE, {"items": [{"bike_unit": unit["id"]}]}, format="json"
        )

        self.assertEqual(response.status_code, 400)

    def test_the_kind_of_sale_cannot_be_changed_on_a_draft(self):
        sale = self.made_bike()

        response = self.client.patch(
            f"{BASE}{sale['id']}/", {"sale_type": "stock"}, format="json"
        )

        self.assertEqual(response.status_code, 400)

    def test_full_payment_bike_sale_can_be_paid_in_parts_as_credit(self):
        sale = self.made_bike(due_date=str(timezone.localdate() + timedelta(days=15)))
        self.pay(sale["id"], "60000.00")
        self.confirm(sale["id"])

        figures = self.figures()
        listed = self.followups(kind="credit_due")

        self.assertEqual(figures["credit_to_receive"], "40000.00")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["amount"], "40000.00")
        self.assertEqual(
            listed[0]["due_date"], str(timezone.localdate() + timedelta(days=15))
        )

    def test_sales_are_filtered_by_type_and_searched_by_chassis(self):
        self.made_bike()
        self.made()

        self.assertEqual(self.client.get(BASE, {"sale_type": "bike"}).data["count"], 1)
        self.assertEqual(self.client.get(BASE, {"search": "CH-001"}).data["count"], 1)


class EmiTests(BikeTestCase):

    def test_terms_are_checked_and_the_finance_charge_worked_out(self):
        sale = self.made_bike(**self.emi_terms())

        # 20000 + 10 x 9000 = 110000 against a price of 100000.
        self.assertEqual(sale["payment_mode"], "emi")
        self.assertEqual(sale["finance_charge"], "10000.00")
        self.assertEqual(sale["net_total"], "100000.00")
        self.assertEqual(sale["amount_due"], "110000.00")
        self.assertEqual(sale["remaining_amount"], "110000.00")
        self.assertEqual(
            sale["first_emi_due"],
            str(self._add_month(timezone.localdate())),
        )

    @staticmethod
    def _add_month(day):
        from apps.sales.services import add_months

        return add_months(day, 1)

    def test_no_finance_charge_when_the_instalments_equal_the_price(self):
        sale = self.made_bike(
            payment_mode="emi", down_payment="10000.00",
            emi_months=9, emi_amount="10000.00",
        )

        self.assertEqual(sale["finance_charge"], "0.00")

    def test_emi_rules(self):
        unit = self.unit()

        for extra in (
            {**self.emi_terms(), "customer": None},
            {**self.emi_terms(), "emi_amount": "1000.00"},
            {**self.emi_terms(), "down_payment": "100001.00"},
            {"payment_mode": "emi", "down_payment": "1000"},
            {"payment_mode": "emi", "emi_months": 0, "emi_amount": "10"},
            {"payment_mode": "full", "emi_months": 5, "emi_amount": "10"},
            {"payment_mode": "full", "down_payment": "10"},
        ):
            response = self.bike_sale(units=[unit], **extra)

            self.assertEqual(response.status_code, 400, extra)

    def test_emi_is_for_bike_sales_only(self):
        response = self.create(**self.emi_terms())

        self.assertEqual(response.status_code, 400)

    def test_the_down_payment_must_be_taken_before_confirming(self):
        sale = self.made_bike(**self.emi_terms())
        self.pay(sale["id"], "19999.99")

        refused = self.confirm(sale["id"])
        self.pay(sale["id"], "0.01")
        accepted = self.confirm(sale["id"])

        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.data["code"], "down_payment_due")
        self.assertEqual(accepted.status_code, 200, accepted.data)

    def test_confirming_makes_monthly_instalments_and_follow_ups(self):
        sale = self.made_bike(**self.emi_terms(first_emi_due="2026-01-31"))
        self.pay(sale["id"], "20000.00")
        self.confirm(sale["id"])

        detail = self.detail(sale["id"])
        rows = detail["installments"]

        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["due_date"], "2026-01-31")
        # A short month uses its last day.
        self.assertEqual(rows[1]["due_date"], "2026-02-28")
        self.assertEqual(rows[2]["due_date"], "2026-03-31")
        self.assertEqual({r["amount"] for r in rows}, {"9000.00"})
        self.assertEqual(Followup.objects.filter(kind="emi_due").count(), 10)
        self.assertEqual(Followup.objects.filter(kind="credit_due").count(), 0)

    def test_payments_cover_the_instalments_in_order(self):
        sale = self.made_bike(**self.emi_terms())
        self.pay(sale["id"], "20000.00")
        self.confirm(sale["id"])

        self.pay(sale["id"], "9000.00")
        self.pay(sale["id"], "4000.00")

        rows = self.detail(sale["id"])["installments"]
        states = [(r["paid_amount"], r["status"]) for r in rows[:3]]

        self.assertEqual(states[0], ("9000.00", "paid"))
        self.assertEqual(states[1], ("4000.00", "partial"))
        self.assertEqual(states[2], ("0.00", "upcoming"))

    def test_instalments_past_their_date_are_overdue(self):
        sale = self.made_bike(**self.emi_terms())
        self.pay(sale["id"], "20000.00")
        self.confirm(sale["id"])

        later = timezone.localdate() + timedelta(days=40)

        with mock.patch("django.utils.timezone.localdate", return_value=later):
            rows = self.detail(sale["id"])["installments"]
            overdue = self.followups(overdue="true")

        self.assertEqual(rows[0]["status"], "overdue")
        self.assertEqual(rows[1]["status"], "upcoming")
        self.assertEqual(len(overdue), 1)
        self.assertTrue(overdue[0]["is_overdue"])

    def test_a_paid_instalment_closes_its_follow_up_and_voiding_reopens_it(self):
        sale = self.made_bike(**self.emi_terms())
        self.pay(sale["id"], "20000.00")
        self.confirm(sale["id"])

        payment = self.pay(sale["id"], "9000.00").json()
        closed = self.followups(kind="emi_due", status="done")

        self.assertEqual(len(closed), 1)
        self.assertTrue(closed[0]["auto_closed"])
        self.assertEqual(closed[0]["installment_number"], 1)

        self.client.post(
            f"{PAYMENTS}{payment['id']}/void/", {"reason": "bounced"}, format="json"
        )

        self.assertEqual(self.followups(kind="emi_due", status="done"), [])
        self.assertEqual(len(self.followups(kind="emi_due", status="pending")), 10)

    def test_the_down_payment_of_a_confirmed_emi_sale_cannot_be_voided(self):
        sale = self.made_bike(**self.emi_terms())
        payment = self.pay(sale["id"], "20000.00").json()
        self.confirm(sale["id"])

        response = self.client.post(
            f"{PAYMENTS}{payment['id']}/void/", {"reason": "x"}, format="json"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "down_payment_due")

    def test_payments_cannot_exceed_the_amount_due_with_the_finance_charge(self):
        sale = self.made_bike(**self.emi_terms())
        self.pay(sale["id"], "20000.00")
        self.confirm(sale["id"])

        over = self.pay(sale["id"], "90000.01")
        exact = self.pay(sale["id"], "90000.00")

        self.assertEqual(over.status_code, 400)
        self.assertEqual(exact.status_code, 201)
        detail = self.detail(sale["id"])
        self.assertEqual(detail["payment_status"], "paid")
        self.assertEqual(detail["remaining_amount"], "0.00")
        self.assertEqual(
            Followup.objects.filter(kind="emi_due", status="done").count(), 10
        )

    def test_customer_credit_includes_the_finance_charge(self):
        sale = self.made_bike(**self.emi_terms())
        self.pay(sale["id"], "20000.00")
        self.confirm(sale["id"])

        figures = self.figures()

        self.assertEqual(figures["total_sales"], "110000.00")
        self.assertEqual(figures["credit_to_receive"], "90000.00")

    def test_a_draft_can_be_switched_back_to_full_payment(self):
        sale = self.made_bike(**self.emi_terms())

        data = self.client.patch(
            f"{BASE}{sale['id']}/",
            {"payment_mode": "full", "down_payment": "0", "emi_months": None,
             "emi_amount": None, "first_emi_due": None},
            format="json",
        )

        self.assertEqual(data.status_code, 200, data.data)
        self.assertEqual(data.json()["amount_due"], "100000.00")

    def test_editing_the_price_recalculates_the_finance_charge(self):
        sale = self.made_bike(**self.emi_terms())

        data = self.client.patch(
            f"{BASE}{sale['id']}/", {"discount_amount": "5000.00"}, format="json"
        ).json()

        self.assertEqual(data["net_total"], "95000.00")
        self.assertEqual(data["finance_charge"], "15000.00")

    def test_sales_are_filtered_by_payment_mode(self):
        self.made_bike(**self.emi_terms())
        self.made()

        self.assertEqual(self.client.get(BASE, {"payment_mode": "emi"}).data["count"], 1)

    def test_database_keeps_emi_fields_in_line_with_the_mode(self):
        base = dict(business=self.ho, sale_date=timezone.localdate())

        with self.assertRaises(IntegrityError), transaction.atomic():
            Sale.objects.create(number=70, sale_type="stock", payment_mode="emi",
                                emi_months=3, emi_amount=Decimal("5"),
                                first_emi_due=date(2026, 1, 1), **base)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Sale.objects.create(number=71, payment_mode="full", emi_months=3, **base)

        with self.assertRaises(IntegrityError), transaction.atomic():
            EmiInstallment.objects.create(
                sale=Sale.objects.create(number=72, **base), number=1,
                due_date=date(2026, 1, 1), amount=Decimal("0"),
            )


class CreditFollowupTests(BikeTestCase):

    def test_a_credit_sale_gets_a_follow_up_30_days_out_by_default(self):
        sale = self.confirmed()

        listed = self.followups()

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["kind"], "credit_due")
        self.assertEqual(listed[0]["amount"], "1500.00")
        self.assertEqual(listed[0]["customer_name"], "Ram Auto")
        self.assertEqual(listed[0]["customer_phone"], "9800000001")
        self.assertEqual(
            listed[0]["due_date"], str(timezone.localdate() + timedelta(days=30))
        )
        self.assertEqual(listed[0]["sale_no"], sale["sale_no"])

    def test_no_follow_up_when_nothing_is_left_to_receive(self):
        sale = self.made()
        self.pay(sale["id"], "1500.00")
        self.confirm(sale["id"])

        self.assertEqual(self.followups(), [])

    def test_paying_the_rest_closes_it_and_voiding_reopens_it(self):
        sale = self.confirmed()
        payment = self.pay(sale["id"], "1500.00").json()

        self.assertEqual(len(self.followups(status="done")), 1)
        self.assertTrue(self.followups(status="done")[0]["auto_closed"])

        self.client.post(
            f"{PAYMENTS}{payment['id']}/void/", {"reason": "x"}, format="json"
        )

        self.assertEqual(len(self.followups(status="pending")), 1)

    def test_a_part_payment_keeps_it_open(self):
        sale = self.confirmed()
        self.pay(sale["id"], "500.00")

        self.assertEqual(len(self.followups(status="pending")), 1)

    def test_a_person_can_mark_it_done_or_skip_it_with_a_note(self):
        one = self.confirmed()
        two = self.confirmed()
        first, second = self.followups()

        done = self.client.post(
            f"{FOLLOWUPS}{first['id']}/done/", {"note": "Will pay Friday"}, format="json"
        )
        skip_without_note = self.client.post(
            f"{FOLLOWUPS}{second['id']}/skip/", {}, format="json"
        )
        skipped = self.client.post(
            f"{FOLLOWUPS}{second['id']}/skip/", {"note": "Customer moved"}, format="json"
        )

        self.assertEqual(done.json()["status"], "done")
        self.assertFalse(done.json()["auto_closed"])
        self.assertEqual(done.json()["note"], "Will pay Friday")
        self.assertEqual(skip_without_note.status_code, 400)
        self.assertEqual(skipped.json()["status"], "skipped")
        self.assertEqual(one["status"], two["status"])

    def test_a_closed_follow_up_cannot_be_closed_again(self):
        self.confirmed()
        followup = self.followups()[0]
        self.client.post(f"{FOLLOWUPS}{followup['id']}/done/", {}, format="json")

        again = self.client.post(f"{FOLLOWUPS}{followup['id']}/done/", {}, format="json")

        self.assertEqual(again.status_code, 409)

    def test_a_manually_closed_follow_up_is_not_reopened_by_a_void(self):
        sale = self.confirmed()
        followup = self.followups()[0]
        self.client.post(f"{FOLLOWUPS}{followup['id']}/done/", {}, format="json")
        payment = self.pay(sale["id"], "10.00").json()

        self.client.post(
            f"{PAYMENTS}{payment['id']}/void/", {"reason": "x"}, format="json"
        )

        self.assertEqual(self.followups(status="pending"), [])

    def test_follow_ups_are_filtered_and_cannot_be_created_or_edited(self):
        self.confirmed()
        listed = self.followups()

        self.assertEqual(self.client.get(FOLLOWUPS, {"customer": self.customer.pk}).data["count"], 1)
        self.assertEqual(
            self.client.get(FOLLOWUPS, {"due_date__lte": "2000-01-01"}).data["count"], 0
        )
        self.assertEqual(self.client.post(FOLLOWUPS, {}, format="json").status_code, 405)
        self.assertEqual(
            self.client.patch(f"{FOLLOWUPS}{listed[0]['id']}/", {}, format="json").status_code, 405
        )


class MaintenanceTests(BikeTestCase):

    def maintenance(self, items=None, **extra):
        payload = {
            "sale_type": "maintenance",
            "customer": self.customer.pk,
            "vehicle_chassis_no": "CH-777",
            "vehicle_reg_no": "BA 1 PA 1234",
            "items": items if items is not None else [
                {"product": self.oil.pk, "quantity": "2"},
                {"description": "Service charge", "unit_price": "500.00", "quantity": "1"},
            ],
            **extra,
        }

        return self.client.post(BASE, payload, format="json")

    def made_maintenance(self, **extra):
        response = self.maintenance(**extra)
        self.assertEqual(response.status_code, 201, response.data)

        return response.json()

    def test_parts_and_service_charges_make_one_total(self):
        sale = self.made_maintenance()

        # 2 x 150 + 500
        self.assertEqual(sale["net_total"], "800.00")
        names = [i["product_name"] for i in sale["items"]]
        self.assertEqual(names, ["Engine Oil", "Service charge"])
        self.assertIsNone(sale["items"][1]["product"])
        self.assertEqual(sale["vehicle_chassis_no"], "CH-777")

    def test_only_parts_take_stock_and_services_do_not(self):
        sale = self.made_maintenance()

        self.confirm(sale["id"])

        self.assertEqual(self.stock(), dec("48"))
        self.assertEqual(StockMovement.objects.filter(movement_type="sale_issue").count(), 1)

    def test_a_service_only_sale_needs_no_stock(self):
        sale = self.made_maintenance(
            items=[{"description": "Washing", "unit_price": "300", "quantity": "1"}]
        )

        response = self.confirm(sale["id"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(StockMovement.objects.filter(movement_type="sale_issue").count(), 0)

    def test_line_rules(self):
        for items in (
            [{"description": "No price", "quantity": "1"}],
            [{"description": "", "unit_price": "10", "quantity": "1"}],
            [{"product": self.oil.pk, "description": "Both", "quantity": "1"}],
            [{"product": self.oil.pk}],
        ):
            self.assertEqual(self.maintenance(items=items).status_code, 400, items)

    def test_service_charges_are_for_maintenance_only(self):
        response = self.create(
            items=[{"description": "Service", "unit_price": "10", "quantity": "1"}]
        )

        self.assertEqual(response.status_code, 400)

    def test_vehicle_details_are_for_maintenance_only(self):
        response = self.create(vehicle_chassis_no="CH-1")

        self.assertEqual(response.status_code, 400)

    def test_confirming_books_a_follow_up_90_days_later(self):
        sale = self.made_maintenance(sale_date=str(timezone.localdate()))
        self.pay(sale["id"], "800.00")
        self.confirm(sale["id"])

        listed = self.followups(kind="maintenance_due")

        self.assertEqual(len(listed), 1)
        self.assertEqual(
            listed[0]["due_date"], str(timezone.localdate() + timedelta(days=90))
        )
        self.assertIsNone(listed[0]["amount"])
        self.assertEqual(listed[0]["vehicle_chassis_no"], "CH-777")
        self.assertEqual(listed[0]["vehicle_reg_no"], "BA 1 PA 1234")
        # Paying never closes a maintenance follow-up.
        self.assertEqual(listed[0]["status"], "pending")

    def test_a_post_service_check_is_booked_3_days_after_the_service(self):
        sale = self.made_maintenance(sale_date=str(timezone.localdate()))
        self.pay(sale["id"], "800.00")
        self.confirm(sale["id"])

        post = self.followups(kind="maintenance_post")
        ninety = self.followups(kind="maintenance_due")

        self.assertEqual(len(post), 1)
        self.assertEqual(
            post[0]["due_date"], str(timezone.localdate() + timedelta(days=3))
        )
        self.assertEqual(post[0]["status"], "pending")
        self.assertFalse(post[0]["has_issue"])
        self.assertIsNone(post[0]["previous_followup"])
        self.assertEqual(len(ninety), 1)

    def test_both_follow_ups_count_from_the_sale_date_and_show_it(self):
        day = timezone.localdate() - timedelta(days=20)
        sale = self.made_maintenance(sale_date=str(day))
        self.pay(sale["id"], "800.00")
        self.confirm(sale["id"])

        post = self.followups(kind="maintenance_post")[0]
        ninety = self.followups(kind="maintenance_due")[0]

        self.assertEqual(post["sale_date"], str(day))
        self.assertEqual(ninety["sale_date"], str(day))
        self.assertEqual(post["due_date"], str(day + timedelta(days=3)))
        self.assertEqual(ninety["due_date"], str(day + timedelta(days=90)))
        self.assertTrue(post["is_overdue"])
        self.assertFalse(ninety["is_overdue"])

    def test_an_issue_found_in_the_3_day_check_is_shown_on_the_90_day_one(self):
        sale = self.made_maintenance()
        self.pay(sale["id"], "800.00")
        self.confirm(sale["id"])
        post = self.followups(kind="maintenance_post")[0]

        before = self.followups(kind="maintenance_due")[0]
        self.assertEqual(before["previous_followup"]["status"], "pending")
        self.assertFalse(before["previous_followup"]["has_issue"])

        response = self.client.post(
            f"{FOLLOWUPS}{post['id']}/done/",
            {"note": "Chain noise again", "has_issue": True},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.json()["has_issue"])
        previous = self.followups(kind="maintenance_due")[0]["previous_followup"]
        self.assertEqual(previous["status"], "done")
        self.assertTrue(previous["has_issue"])
        self.assertEqual(previous["note"], "Chain noise again")

    def test_a_check_with_no_issue_is_recorded_as_such(self):
        sale = self.made_maintenance()
        self.pay(sale["id"], "800.00")
        self.confirm(sale["id"])
        post = self.followups(kind="maintenance_post")[0]

        self.client.post(f"{FOLLOWUPS}{post['id']}/done/", {"note": "All good"}, format="json")

        previous = self.followups(kind="maintenance_due")[0]["previous_followup"]
        self.assertFalse(previous["has_issue"])
        self.assertEqual(previous["note"], "All good")

    def test_an_issue_cannot_be_recorded_on_a_money_follow_up(self):
        sale = self.made_maintenance()
        self.confirm(sale["id"])
        credit = self.followups(kind="credit_due")[0]

        self.client.post(
            f"{FOLLOWUPS}{credit['id']}/done/", {"has_issue": True}, format="json"
        )

        self.assertFalse(self.followups(kind="credit_due")[0]["has_issue"])

    def test_paying_never_closes_a_post_service_check(self):
        sale = self.made_maintenance()
        self.confirm(sale["id"])
        self.pay(sale["id"], "800.00")

        self.assertEqual(self.followups(kind="maintenance_post")[0]["status"], "pending")

    def test_a_walk_in_maintenance_sale_still_gets_a_follow_up(self):
        sale = self.made_maintenance(customer=None, walk_in_name="Hari")
        self.pay(sale["id"], "800.00")
        self.confirm(sale["id"])

        listed = self.followups(kind="maintenance_due")

        self.assertEqual(listed[0]["customer_name"], "Hari")

    def test_a_credit_maintenance_sale_gets_both_follow_ups(self):
        sale = self.made_maintenance()
        self.pay(sale["id"], "300.00")
        self.confirm(sale["id"])

        kinds = sorted(f["kind"] for f in self.followups())

        self.assertEqual(
            kinds, ["credit_due", "maintenance_due", "maintenance_post"]
        )

    def test_done_can_book_the_next_follow_up(self):
        sale = self.made_maintenance()
        self.pay(sale["id"], "800.00")
        self.confirm(sale["id"])
        followup = self.followups(kind="maintenance_due")[0]
        next_day = str(timezone.localdate() + timedelta(days=90))

        response = self.client.post(
            f"{FOLLOWUPS}{followup['id']}/done/",
            {"note": "Customer visited", "next_due_date": next_day},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        pending = self.followups(kind="maintenance_due", status="pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["due_date"], next_day)

    def test_next_follow_up_rules(self):
        sale = self.made_maintenance()
        self.confirm(sale["id"])
        credit = self.followups(kind="credit_due")[0]
        maintenance = self.followups(kind="maintenance_due")[0]

        past = self.client.post(
            f"{FOLLOWUPS}{maintenance['id']}/done/",
            {"next_due_date": str(timezone.localdate())}, format="json",
        )
        wrong_kind = self.client.post(
            f"{FOLLOWUPS}{credit['id']}/done/",
            {"next_due_date": str(timezone.localdate() + timedelta(days=5))},
            format="json",
        )

        self.assertEqual(past.status_code, 400)
        self.assertEqual(wrong_kind.status_code, 400)

    def test_a_new_maintenance_sale_books_its_own_follow_up(self):
        for _ in range(2):
            sale = self.made_maintenance()
            self.pay(sale["id"], "800.00")
            self.confirm(sale["id"])

        self.assertEqual(len(self.followups(kind="maintenance_due")), 2)
        self.assertEqual(len(self.followups(kind="maintenance_post")), 2)

    def test_a_cancelled_draft_creates_no_follow_up(self):
        sale = self.made_maintenance()
        self.client.post(f"{BASE}{sale['id']}/cancel/", {"reason": "x"}, format="json")

        self.assertEqual(self.followups(), [])


class IncomeTests(BikeTestCase):

    def test_every_payment_is_added_to_income_in_its_sale_category(self):
        stock = self.confirmed()
        bike = self.made_bike()
        maintenance = self.client.post(
            BASE,
            {"sale_type": "maintenance", "customer": self.customer.pk,
             "items": [{"description": "Service", "unit_price": "500", "quantity": "1"}]},
            format="json",
        ).json()

        self.pay(stock["id"], "100.00", method="bank", reference="T-1")
        self.pay(bike["id"], "2000.00")
        self.pay(maintenance["id"], "500.00")

        summary = self.client.get(f"{INCOME}summary/").json()
        rows = {r["category"]: r["total"] for r in summary["by_category"]}

        self.assertEqual(summary["total"], "2600.00")
        self.assertEqual(rows, {
            "stock_sale": "100.00", "bike_sale": "2000.00", "maintenance": "500.00",
        })

        entry = Income.objects.get(category="stock_sale")
        self.assertEqual(entry.payment_method, "bank")
        self.assertEqual(entry.reference, "T-1")
        self.assertEqual(entry.business, self.ho)
        self.assertEqual(entry.created_by, self.owner)
        self.assertIn(stock["sale_no"], entry.description)

    def test_voiding_a_payment_voids_its_income(self):
        sale = self.confirmed()
        payment = self.pay(sale["id"], "100.00").json()

        self.client.post(
            f"{PAYMENTS}{payment['id']}/void/", {"reason": "x"}, format="json"
        )

        self.assertEqual(self.client.get(f"{INCOME}summary/").json()["total"], "0.00")
        self.assertEqual(Income.objects.get().status, "void")

    def test_sale_income_is_locked(self):
        sale = self.confirmed()
        self.pay(sale["id"], "100.00")
        income = self.client.get(INCOME).json()["results"][0]

        response = self.client.post(
            f"{INCOME}{income['id']}/void/", {"reason": "x"}, format="json"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "payment_income_locked")

    def test_other_income_is_entered_and_voided_by_the_owner(self):
        created = self.client.post(
            INCOME,
            {"category": "other", "amount": "700.00", "description": "Scrap sale"},
            format="json",
        )

        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.json()["income_date"], str(timezone.localdate()))

        voided = self.client.post(
            f"{INCOME}{created.json()['id']}/void/", {"reason": "typo"}, format="json"
        )
        again = self.client.post(
            f"{INCOME}{created.json()['id']}/void/", {"reason": "x"}, format="json"
        )

        self.assertEqual(voided.json()["status"], "void")
        self.assertEqual(again.status_code, 409)

    def test_manual_income_input_rules(self):
        for payload in (
            {"category": "stock_sale", "amount": "10"},
            {"category": "other", "amount": "0"},
            {"category": "other", "amount": "10", "income_date": "2999-01-01"},
            {"category": "other", "amount": "10", "payment_method": "barter"},
        ):
            self.assertEqual(
                self.client.post(INCOME, payload, format="json").status_code, 400, payload
            )

    def test_income_is_filtered_and_owner_only(self):
        sale = self.confirmed()
        self.pay(sale["id"], "100.00")
        self.client.post(INCOME, {"category": "other", "amount": "5"}, format="json")

        self.assertEqual(self.client.get(INCOME, {"category": "other"}).data["count"], 1)
        self.assertEqual(
            self.client.get(INCOME, {"income_date__gte": "2999-01-01"}).data["count"], 0
        )

        position = self.make_position(self.ho, "Cashier", sales=("view", "create"))
        self.make_user("cashier", User.Role.STAFF, company=self.company, position=position)
        self.authenticate("ACME-HO", "cashier")

        self.assertEqual(self.client.get(INCOME).status_code, 403)

    def test_income_of_another_business_is_hidden(self):
        sale = self.confirmed()
        self.pay(sale["id"], "100.00")

        self.authenticate("OTHER-HO", "owner")

        self.assertEqual(self.client.get(INCOME).data["count"], 0)
        self.assertEqual(self.client.get(f"{INCOME}summary/").json()["total"], "0.00")


class BikeAccessTests(BikeTestCase):

    def test_staff_follow_their_position_for_bikes_and_follow_ups(self):
        sale = self.confirmed()
        followup = self.followups()[0]
        position = self.make_position(self.ho, "Viewer", sales=("view",))
        self.make_user("viewer", User.Role.STAFF, company=self.company, position=position)
        position = self.make_position(self.ho, "Seller", sales=("view", "create"))
        self.make_user("seller", User.Role.STAFF, company=self.company, position=position)

        self.authenticate("ACME-HO", "viewer")
        self.assertEqual(self.client.get(BIKES).status_code, 200)
        self.assertEqual(self.client.get(FOLLOWUPS).status_code, 200)
        self.assertEqual(
            self.client.post(BIKES, {"product": self.model.pk, "chassis_no": "Z"}, format="json").status_code,
            403,
        )
        self.assertEqual(
            self.client.post(f"{FOLLOWUPS}{followup['id']}/done/", {}, format="json").status_code,
            403,
        )

        self.authenticate("ACME-HO", "seller")
        self.assertEqual(
            self.client.post(f"{FOLLOWUPS}{followup['id']}/done/", {}, format="json").status_code,
            200,
        )
        self.assertTrue(sale)

    def test_another_company_cannot_touch_follow_ups(self):
        self.confirmed()
        followup = self.followups()[0]

        self.authenticate("OTHER-HO", "owner")

        self.assertEqual(self.client.get(FOLLOWUPS).data["count"], 0)
        self.assertEqual(
            self.client.post(f"{FOLLOWUPS}{followup['id']}/done/", {}, format="json").status_code,
            404,
        )

    def test_the_customer_list_still_shows_credit_after_a_bike_emi_sale(self):
        sale = self.made_bike(**self.emi_terms())
        self.pay(sale["id"], "20000.00")
        self.confirm(sale["id"])

        listed = self.client.get(CUSTOMERS).json()["results"]
        ram = next(c for c in listed if c["id"] == self.customer.pk)

        self.assertEqual(ram["credit_to_receive"], "90000.00")
