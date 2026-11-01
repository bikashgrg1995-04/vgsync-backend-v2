from datetime import date
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError, transaction
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.common.testing import TenantTestMixin
from apps.finance.models import Expense
from apps.payroll.models import PayrollEntry, PayrollPayment, SalaryProfile

SALARIES_URL = "/api/v1/payroll/salaries/"
BALANCES_URL = "/api/v1/payroll/balances/"
ENTRIES_URL = "/api/v1/payroll/entries/"
PAYMENTS_URL = "/api/v1/payroll/payments/"
EXPENSES_URL = "/api/v1/finance/expenses/"


def money(value):
    return Decimal(str(value))


class PayrollTestCase(TenantTestMixin, APITestCase):

    def setUp(self):
        super().setUp()

        self.position = self.make_position(self.ho, "Sales", sales=("view",))
        self.ram = self.make_user(
            "ram", User.Role.STAFF, company=self.company, position=self.position,
            first_name="Ram", last_name="Thapa",
        )
        self.sita = self.make_user(
            "sita", User.Role.STAFF, company=self.company, position=self.position,
        )
        self.authenticate("ACME-HO", "owner")
        self._clock = None
        self.today(date(2026, 1, 1))

    def today(self, day):
        """Pretend it is ``day``."""
        if self._clock:
            self._clock.stop()

        self._clock = mock.patch("django.utils.timezone.localdate", return_value=day)
        self._clock.start()
        self.addCleanup(self._clock.stop)

    def set_pay(self, staff, pay_type="monthly", **fields):
        payload = {"pay_type": pay_type, **fields}

        if pay_type == "monthly":
            payload.setdefault("rate", "30000.00")

        return self.client.put(
            f"{SALARIES_URL}{staff.pk}/", payload, format="json"
        )

    def monthly(self, staff=None, rate="30000.00", allowances="2000.00", day=5,
                start="2026-01-01"):
        response = self.set_pay(
            staff or self.ram, rate=rate, allowances=allowances,
            salary_day=day, start_date=start,
        )
        self.assertIn(response.status_code, (200, 201), response.data)

        return response

    def daily(self, staff=None):
        response = self.set_pay(staff or self.sita, "daily")
        self.assertIn(response.status_code, (200, 201), response.data)

        return response

    def wage(self, staff, day, amount, **extra):
        return self.client.post(
            ENTRIES_URL,
            {"staff": staff.pk, "kind": "wage", "entry_date": day,
             "amount": amount, **extra},
            format="json",
        )

    def entry(self, staff, kind, day, amount):
        return self.client.post(
            ENTRIES_URL,
            {"staff": staff.pk, "kind": kind, "entry_date": day, "amount": amount},
            format="json",
        )

    def pay(self, staff, amount, **extra):
        payload = {
            "staff": staff.pk, "amount": amount, "method": "cash", **extra
        }

        return self.client.post(PAYMENTS_URL, payload, format="json")

    def balance(self, staff):
        response = self.client.get(f"{BALANCES_URL}{staff.pk}/")
        self.assertEqual(response.status_code, 200, response.data)

        # The JSON the app receives: money as strings, dates as ISO text.
        return response.json()


class PaySetupTests(PayrollTestCase):

    def test_monthly_pay_is_set_and_changed(self):
        created = self.monthly()

        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.data["pay_type"], "monthly")
        self.assertEqual(money(created.data["rate"]), money("30000"))
        self.assertEqual(created.data["salary_day"], 5)
        self.assertEqual(created.data["staff_name"], "Ram Thapa")

        updated = self.set_pay(self.ram, rate="35000.00")

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(SalaryProfile.objects.filter(user=self.ram).count(), 1)
        self.assertEqual(SalaryProfile.objects.get(user=self.ram).updated_by, self.owner)

    def test_start_date_defaults_to_today(self):
        created = self.set_pay(self.ram)

        self.assertEqual(created.data["start_date"], "2026-01-01")

    def test_monthly_needs_a_rate(self):
        response = self.client.put(
            f"{SALARIES_URL}{self.ram.pk}/", {"pay_type": "monthly"}, format="json"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("rate", response.data)

    def test_daily_pay_has_no_fixed_rate_or_allowances(self):
        with_rate = self.set_pay(self.sita, "daily", rate="800.00")
        with_allowance = self.set_pay(self.sita, "daily", allowances="100.00")
        ok = self.set_pay(self.sita, "daily")

        self.assertEqual(with_rate.status_code, 400)
        self.assertEqual(with_allowance.status_code, 400)
        self.assertEqual(ok.status_code, 201, ok.data)
        self.assertIsNone(ok.data["rate"])

    def test_hourly_pay_is_not_offered(self):
        self.assertEqual(self.set_pay(self.ram, "hourly", rate="100").status_code, 400)

    def test_bad_values_are_rejected(self):
        self.assertEqual(self.set_pay(self.ram, rate="-1").status_code, 400)
        self.assertEqual(self.set_pay(self.ram, salary_day=0).status_code, 400)
        self.assertEqual(self.set_pay(self.ram, salary_day=32).status_code, 400)
        self.assertEqual(self.set_pay(self.ram, "weekly").status_code, 400)
        self.assertFalse(SalaryProfile.objects.exists())

    def test_start_date_cannot_be_changed_later(self):
        self.monthly(start="2026-01-01")

        response = self.set_pay(self.ram, start_date="2025-06-01")

        self.assertEqual(response.status_code, 400)

    def test_list_is_scoped_and_paginated(self):
        self.monthly()
        self.daily()

        self.assertEqual(self.client.get(SALARIES_URL).data["count"], 2)
        self.assertEqual(
            self.client.get(SALARIES_URL, {"pay_type": "daily"}).data["count"], 1
        )

        self.authenticate("OTHER-HO", "owner")
        self.assertEqual(self.client.get(SALARIES_URL).data["count"], 0)

    def test_cannot_touch_another_companys_staff(self):
        self.monthly()
        self.authenticate("OTHER-HO", "owner")

        self.assertEqual(self.client.get(f"{SALARIES_URL}{self.ram.pk}/").status_code, 404)
        self.assertEqual(self.set_pay(self.ram, rate="1.00").status_code, 404)

    def test_the_owner_has_no_pay_here(self):
        self.assertEqual(self.set_pay(self.owner).status_code, 404)

    def test_switching_pay_type_starts_from_today(self):
        self.monthly()
        self.today(date(2026, 3, 10))

        response = self.set_pay(self.ram, "daily")

        self.assertEqual(response.status_code, 200, response.data)
        # Jan..Mar salary due before the switch was recorded first.
        self.assertEqual(
            PayrollEntry.objects.filter(staff=self.ram, kind="salary").count(), 3
        )
        profile = SalaryProfile.objects.get(user=self.ram)
        self.assertEqual(profile.start_date, date(2026, 3, 10))


class MonthlySalaryTests(PayrollTestCase):

    def salary_entries(self, staff=None):
        return list(
            PayrollEntry.objects.filter(staff=staff or self.ram, kind="salary")
            .order_by("entry_date")
        )

    def test_nothing_is_due_before_the_salary_day(self):
        self.monthly(day=5)
        self.today(date(2026, 1, 4))

        self.assertEqual(self.balance(self.ram)["remaining"], "0.00")
        self.assertEqual(self.salary_entries(), [])

    def test_salary_falls_due_on_the_salary_day_with_allowances(self):
        self.monthly(day=5)
        self.today(date(2026, 1, 5))

        data = self.balance(self.ram)

        self.assertEqual(money(data["remaining"]), money("32000"))
        self.assertEqual(money(data["current_remaining"]), money("32000"))
        self.assertEqual(money(data["previous_remaining"]), money("0"))
        self.assertEqual(data["cycle_start"], "2026-01-05")
        self.assertEqual(data["next_salary_date"], "2026-02-05")

    def test_every_missed_month_is_recorded_once(self):
        self.monthly(day=5)
        self.today(date(2026, 3, 10))

        self.balance(self.ram)
        self.balance(self.ram)
        self.client.get(BALANCES_URL)

        dates = [e.entry_date for e in self.salary_entries()]
        self.assertEqual(
            dates, [date(2026, 1, 5), date(2026, 2, 5), date(2026, 3, 5)]
        )

    def test_short_months_use_their_last_day(self):
        self.monthly(day=31)
        self.today(date(2026, 3, 1))

        self.balance(self.ram)
        dates = [e.entry_date for e in self.salary_entries()]

        self.assertEqual(dates, [date(2026, 1, 31), date(2026, 2, 28)])

    def test_a_rate_change_applies_to_future_months_only(self):
        self.monthly(rate="30000.00", allowances="0.00", day=5)
        self.today(date(2026, 2, 10))

        self.set_pay(self.ram, rate="40000.00", allowances="0.00")
        self.today(date(2026, 3, 6))
        self.balance(self.ram)

        amounts = [money(e.amount) for e in self.salary_entries()]
        self.assertEqual(amounts, [money("30000"), money("30000"), money("40000")])

    def test_inactive_staff_earn_no_salary_and_reactivation_does_not_back_pay(self):
        self.monthly(day=5)
        self.today(date(2026, 2, 10))

        deleted = self.client.delete(f"/api/v1/staff/{self.ram.pk}/")
        self.assertEqual(deleted.status_code, 204)

        # Jan and Feb were recorded at the moment of deactivation.
        self.assertEqual(len(self.salary_entries()), 2)

        self.today(date(2026, 5, 10))
        self.client.get(BALANCES_URL)
        self.assertEqual(len(self.salary_entries()), 2)

        self.client.post(f"/api/v1/staff/{self.ram.pk}/reactivate/")
        self.today(date(2026, 6, 6))
        self.balance(self.ram)

        self.assertEqual(
            [e.entry_date for e in self.salary_entries()][-1], date(2026, 6, 5)
        )
        self.assertEqual(len(self.salary_entries()), 3)

    def test_a_voided_month_is_not_recreated(self):
        self.monthly(day=5)
        self.today(date(2026, 1, 6))
        self.balance(self.ram)
        entry = self.salary_entries()[0]

        self.client.post(
            f"{ENTRIES_URL}{entry.pk}/void/", {"reason": "joined late"}, format="json"
        )

        self.assertEqual(self.balance(self.ram)["remaining"], "0.00")
        self.assertEqual(
            PayrollEntry.objects.filter(staff=self.ram, status="active").count(), 0
        )


class PaymentTests(PayrollTestCase):

    def test_full_payment_clears_the_balance(self):
        self.monthly(day=5)
        self.today(date(2026, 1, 5))

        response = self.pay(self.ram, "32000.00")

        self.assertEqual(response.status_code, 201, response.data)
        data = self.balance(self.ram)
        self.assertEqual(money(data["remaining"]), money("0"))
        self.assertEqual(money(data["total_paid"]), money("32000"))
        self.assertEqual(data["unsettled"], [])

    def test_part_payment_shows_what_is_left(self):
        self.monthly(day=5)
        self.today(date(2026, 1, 5))

        self.pay(self.ram, "12000.00")
        data = self.balance(self.ram)

        self.assertEqual(money(data["remaining"]), money("20000"))
        self.assertEqual(data["unsettled"][0]["state"], "partial")
        self.assertEqual(money(data["unsettled"][0]["paid"]), money("12000"))
        self.assertEqual(data["last_payment_date"], "2026-01-05")

    def test_an_advance_is_absorbed_when_the_salary_falls_due(self):
        self.monthly(day=5)
        self.today(date(2026, 1, 2))

        self.pay(self.ram, "10000.00")
        before = self.balance(self.ram)

        self.assertEqual(money(before["advance"]), money("10000"))
        self.assertEqual(money(before["remaining"]), money("0"))

        self.today(date(2026, 1, 5))
        after = self.balance(self.ram)

        self.assertEqual(money(after["advance"]), money("0"))
        self.assertEqual(money(after["remaining"]), money("22000"))

    def test_paying_more_than_owed_leaves_an_advance(self):
        self.monthly(day=5)
        self.today(date(2026, 1, 5))

        self.pay(self.ram, "40000.00")
        data = self.balance(self.ram)

        self.assertEqual(money(data["remaining"]), money("0"))
        self.assertEqual(money(data["advance"]), money("8000"))

    def test_unpaid_amount_of_an_earlier_month_is_carried_over(self):
        self.monthly(day=5)
        self.today(date(2026, 1, 20))
        self.pay(self.ram, "30000.00")

        self.today(date(2026, 2, 6))
        data = self.balance(self.ram)

        # Jan has 2000 left; Feb's 32000 is the new period.
        self.assertEqual(money(data["previous_remaining"]), money("2000"))
        self.assertEqual(money(data["current_remaining"]), money("32000"))
        self.assertEqual(money(data["remaining"]), money("34000"))
        self.assertEqual(data["cycle_start"], "2026-02-05")
        carried = {u["entry_date"]: u["carried_over"] for u in data["unsettled"]}
        self.assertEqual(carried, {"2026-01-05": True, "2026-02-05": False})

    def test_payments_settle_the_oldest_amount_first(self):
        self.monthly(day=5)
        self.today(date(2026, 2, 6))
        self.pay(self.ram, "32000.00")

        data = self.balance(self.ram)

        self.assertEqual(money(data["previous_remaining"]), money("0"))
        self.assertEqual(money(data["current_remaining"]), money("32000"))
        self.assertEqual([u["entry_date"] for u in data["unsettled"]], ["2026-02-05"])

    def test_a_payment_also_becomes_a_salary_expense(self):
        self.monthly(day=5)
        self.today(date(2026, 1, 5))

        payment_id = self.pay(self.ram, "5000.00", reference="TX1").data["id"]

        expense = Expense.objects.get()
        self.assertEqual(expense.category, "salary")
        self.assertEqual(money(expense.amount), money("5000"))
        self.assertEqual(expense.expense_date, date(2026, 1, 5))
        self.assertEqual(expense.business, self.ho)
        self.assertEqual(expense.payroll_payment_id, payment_id)
        self.assertEqual(expense.reference, "TX1")

    def test_voiding_a_payment_restores_the_balance_and_voids_the_expense(self):
        self.monthly(day=5)
        self.today(date(2026, 1, 5))
        payment_id = self.pay(self.ram, "32000.00").data["id"]

        response = self.client.post(
            f"{PAYMENTS_URL}{payment_id}/void/", {"reason": "wrong staff"}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "void")
        self.assertEqual(money(self.balance(self.ram)["remaining"]), money("32000"))
        self.assertEqual(Expense.objects.get().status, "void")

        again = self.client.post(
            f"{PAYMENTS_URL}{payment_id}/void/", {"reason": "x"}, format="json"
        )
        self.assertEqual(again.status_code, 409)

    def test_a_reason_is_needed_to_void(self):
        self.monthly()
        payment_id = self.pay(self.ram, "100.00").data["id"]

        response = self.client.post(f"{PAYMENTS_URL}{payment_id}/void/", {}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_payment_rules(self):
        self.monthly()

        self.assertEqual(self.pay(self.ram, "0").status_code, 400)
        self.assertEqual(self.pay(self.ram, "-5").status_code, 400)
        self.assertEqual(self.pay(self.ram, "10", paid_on="2026-01-02").status_code, 400)
        self.assertEqual(self.pay(self.ram, "10", method="cheque").status_code, 400)
        # No pay set up for sita yet.
        self.assertEqual(self.pay(self.sita, "10").status_code, 400)
        self.assertEqual(self.pay(self.owner, "10").status_code, 400)
        self.assertFalse(PayrollPayment.objects.exists())

    def test_payments_cannot_be_edited_or_deleted(self):
        self.monthly()
        payment_id = self.pay(self.ram, "100.00").data["id"]

        self.assertEqual(
            self.client.patch(
                f"{PAYMENTS_URL}{payment_id}/", {"amount": "1"}, format="json"
            ).status_code,
            405,
        )
        self.assertEqual(self.client.delete(f"{PAYMENTS_URL}{payment_id}/").status_code, 405)

    def test_payments_can_be_listed_and_filtered(self):
        self.monthly()
        self.daily()
        self.pay(self.ram, "100.00")
        self.pay(self.sita, "50.00")

        listed = self.client.get(PAYMENTS_URL, {"staff": self.sita.pk})

        self.assertEqual(listed.data["count"], 1)
        self.assertEqual(listed.data["results"][0]["staff_name"], "sita")


class DailyWageTests(PayrollTestCase):

    def setUp(self):
        super().setUp()
        self.daily()

    def test_the_amount_is_entered_each_day(self):
        self.today(date(2026, 1, 10))

        response = self.wage(self.sita, "2026-01-10", "850.00", description="Counter")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(money(self.balance(self.sita)["remaining"]), money("850"))

    def test_unpaid_wage_is_carried_into_the_next_day(self):
        self.today(date(2026, 1, 10))
        self.wage(self.sita, "2026-01-10", "800.00")
        self.pay(self.sita, "500.00")

        self.today(date(2026, 1, 11))
        self.wage(self.sita, "2026-01-11", "900.00")
        data = self.balance(self.sita)

        self.assertEqual(money(data["previous_remaining"]), money("300"))
        self.assertEqual(money(data["current_remaining"]), money("900"))
        self.assertEqual(money(data["remaining"]), money("1200"))
        self.assertEqual(data["cycle_start"], "2026-01-11")

    def test_paid_days_do_not_carry_over(self):
        self.today(date(2026, 1, 10))
        self.wage(self.sita, "2026-01-10", "800.00")
        self.pay(self.sita, "800.00")

        self.today(date(2026, 1, 11))

        data = self.balance(self.sita)
        self.assertEqual(money(data["remaining"]), money("0"))
        self.assertEqual(money(data["previous_remaining"]), money("0"))

    def test_one_wage_per_day_but_it_can_be_edited(self):
        self.today(date(2026, 1, 10))
        entry_id = self.wage(self.sita, "2026-01-10", "800.00").data["id"]

        again = self.wage(self.sita, "2026-01-10", "900.00")
        edited = self.client.patch(
            f"{ENTRIES_URL}{entry_id}/", {"amount": "950.00"}, format="json"
        )

        self.assertEqual(again.status_code, 400)
        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertEqual(money(self.balance(self.sita)["remaining"]), money("950"))

    def test_a_voided_day_can_be_entered_again(self):
        self.today(date(2026, 1, 10))
        entry_id = self.wage(self.sita, "2026-01-10", "800.00").data["id"]

        self.client.post(f"{ENTRIES_URL}{entry_id}/void/", {"reason": "typo"}, format="json")

        self.assertEqual(self.wage(self.sita, "2026-01-10", "600.00").status_code, 201)
        self.assertEqual(money(self.balance(self.sita)["remaining"]), money("600"))

    def test_a_voided_entry_cannot_be_edited(self):
        self.today(date(2026, 1, 10))
        entry_id = self.wage(self.sita, "2026-01-10", "800.00").data["id"]
        self.client.post(f"{ENTRIES_URL}{entry_id}/void/", {"reason": "typo"}, format="json")

        response = self.client.patch(
            f"{ENTRIES_URL}{entry_id}/", {"amount": "1"}, format="json"
        )

        self.assertEqual(response.status_code, 400)

    def test_wages_are_only_for_daily_staff(self):
        self.monthly()

        response = self.wage(self.ram, "2026-01-01", "800.00")

        self.assertEqual(response.status_code, 400)

    def test_entry_rules(self):
        self.assertEqual(self.wage(self.sita, "2026-01-01", "0").status_code, 400)
        self.assertEqual(self.wage(self.sita, "2026-01-01", "-5").status_code, 400)
        self.assertEqual(self.wage(self.sita, "2026-01-02", "100").status_code, 400)
        self.assertEqual(
            self.entry(self.sita, "salary", "2026-01-01", "100").status_code, 400
        )
        # Staff with no pay set up.
        self.assertEqual(self.wage(self.ram, "2026-01-01", "100").status_code, 400)

    def test_bonus_adds_and_deduction_reduces_what_is_owed(self):
        self.today(date(2026, 1, 10))
        self.wage(self.sita, "2026-01-10", "800.00")

        self.assertEqual(self.entry(self.sita, "bonus", "2026-01-10", "200").status_code, 201)
        self.assertEqual(
            self.entry(self.sita, "deduction", "2026-01-10", "150").status_code, 201
        )

        data = self.balance(self.sita)
        self.assertEqual(money(data["total_earned"]), money("1000"))
        self.assertEqual(money(data["total_deducted"]), money("150"))
        self.assertEqual(money(data["remaining"]), money("850"))

    def test_entries_can_be_listed_and_filtered(self):
        self.today(date(2026, 1, 10))
        self.wage(self.sita, "2026-01-09", "800.00")
        self.wage(self.sita, "2026-01-10", "900.00")
        self.entry(self.sita, "bonus", "2026-01-10", "50")

        by_kind = self.client.get(ENTRIES_URL, {"kind": "wage"})
        by_date = self.client.get(ENTRIES_URL, {"entry_date__gte": "2026-01-10"})

        self.assertEqual(by_kind.data["count"], 2)
        self.assertEqual(by_date.data["count"], 2)

    def test_monthly_salary_entries_are_locked(self):
        self.monthly(day=1)
        self.today(date(2026, 1, 2))
        self.client.get(ENTRIES_URL)
        entry = PayrollEntry.objects.get(staff=self.ram, kind="salary")

        response = self.client.patch(
            f"{ENTRIES_URL}{entry.pk}/", {"amount": "1"}, format="json"
        )

        self.assertEqual(response.status_code, 400)


class BalanceListTests(PayrollTestCase):

    def setUp(self):
        super().setUp()
        self.monthly(day=5)
        self.daily()
        self.today(date(2026, 1, 10))
        self.wage(self.sita, "2026-01-10", "800.00")

    def test_lists_everyone_with_pay_set_up_biggest_owed_first(self):
        response = self.client.get(BALANCES_URL)

        self.assertEqual(response.data["count"], 2)
        self.assertEqual(
            [r["staff"] for r in response.data["results"]], [self.ram.pk, self.sita.pk]
        )
        self.assertEqual(money(response.data["results"][0]["remaining"]), money("32000"))

    def test_owing_and_advance_filters(self):
        self.pay(self.ram, "40000.00")

        owing = self.client.get(BALANCES_URL, {"owing": "true"})
        advance = self.client.get(BALANCES_URL, {"advance": "true"})

        self.assertEqual([r["staff"] for r in owing.data["results"]], [self.sita.pk])
        self.assertEqual([r["staff"] for r in advance.data["results"]], [self.ram.pk])

    def test_pay_type_filter_and_search(self):
        daily = self.client.get(BALANCES_URL, {"pay_type": "daily"})
        found = self.client.get(BALANCES_URL, {"search": "Thapa"})

        self.assertEqual([r["staff"] for r in daily.data["results"]], [self.sita.pk])
        self.assertEqual([r["staff"] for r in found.data["results"]], [self.ram.pk])

    def test_people_without_pay_set_up_have_no_balance(self):
        self.assertEqual(
            self.client.get(f"{BALANCES_URL}{self.owner.pk}/").status_code, 404
        )

        bina = self.make_user(
            "bina", User.Role.STAFF, company=self.company, position=self.position
        )
        response = self.client.get(f"{BALANCES_URL}{bina.pk}/")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["code"], "no_salary")

    def test_nothing_leaks_between_companies(self):
        self.authenticate("OTHER-HO", "owner")

        self.assertEqual(self.client.get(BALANCES_URL).data["count"], 0)
        self.assertEqual(self.client.get(f"{BALANCES_URL}{self.ram.pk}/").status_code, 404)
        self.assertEqual(self.client.get(ENTRIES_URL).data["count"], 0)
        self.assertEqual(self.pay(self.ram, "10").status_code, 400)


class PayrollAccessTests(PayrollTestCase):

    def test_only_the_owner_can_use_payroll(self):
        self.make_user("adm", User.Role.ADMIN, company=self.company)
        self.make_user("acc", User.Role.ACCOUNTANT, company=self.company)

        for username in ("adm", "acc", "ram"):
            self.authenticate("ACME-HO", username, password=self.password)

            for url in (SALARIES_URL, BALANCES_URL, ENTRIES_URL, PAYMENTS_URL, EXPENSES_URL):
                self.assertEqual(self.client.get(url).status_code, 403, (username, url))

            self.assertEqual(self.client.post(PAYMENTS_URL, {}, format="json").status_code, 403)

    def test_unauthenticated_requests_are_rejected(self):
        self.client.credentials()

        self.assertEqual(self.client.get(BALANCES_URL).status_code, 401)

    def test_company_owner_is_read_only_in_a_branch(self):
        self.authenticate("ACME-BR", "owner")

        self.assertEqual(self.client.get(BALANCES_URL).status_code, 200)
        self.assertEqual(self.set_pay(self.ram).status_code, 403)
        self.assertEqual(self.pay(self.ram, "10").status_code, 403)

    def test_branch_owner_runs_pay_for_the_branch_team(self):
        self.make_user("bowner", User.Role.OWNER, business=self.branch)
        position = self.make_position(self.branch, "Branch Sales")
        bina = self.make_user(
            "bina", User.Role.STAFF, business=self.branch, position=position
        )
        self.authenticate("ACME-BR", "bowner")

        self.monthly(staff=bina, rate="18000.00", allowances="0.00", day=1)
        self.today(date(2026, 1, 2))
        paid = self.pay(bina, "5000.00")

        self.assertEqual(paid.status_code, 201, paid.data)
        self.assertEqual(Expense.objects.get().business, self.branch)
        self.assertEqual(money(self.balance(bina)["remaining"]), money("13000"))
        self.assertEqual(PayrollEntry.objects.get(staff=bina).business, self.branch)

        # Company staff are not part of the branch team.
        self.assertEqual(self.set_pay(self.ram).status_code, 404)


class ExpenseTests(PayrollTestCase):

    def expense(self, **extra):
        payload = {
            "category": "rent", "amount": "15000.00",
            "expense_date": "2026-01-01", **extra,
        }

        return self.client.post(EXPENSES_URL, payload, format="json")

    def test_salary_payments_and_other_expenses_share_one_list(self):
        self.monthly()
        self.pay(self.ram, "5000.00")
        self.expense()

        response = self.client.get(EXPENSES_URL)

        self.assertEqual(response.data["count"], 2)
        self.assertEqual(
            {r["category"] for r in response.data["results"]}, {"salary", "rent"}
        )

    def test_manual_expense_is_created_edited_and_voided(self):
        created = self.expense(description="January rent", payment_method="bank")

        self.assertEqual(created.status_code, 201, created.data)
        expense_id = created.data["id"]
        self.assertEqual(Expense.objects.get().created_by, self.owner)

        edited = self.client.patch(
            f"{EXPENSES_URL}{expense_id}/", {"amount": "16000.00"}, format="json"
        )
        voided = self.client.post(
            f"{EXPENSES_URL}{expense_id}/void/", {"reason": "duplicate"}, format="json"
        )
        after = self.client.patch(
            f"{EXPENSES_URL}{expense_id}/", {"amount": "1"}, format="json"
        )

        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertEqual(voided.data["status"], "void")
        self.assertEqual(after.status_code, 409)

    def test_date_defaults_to_today(self):
        created = self.client.post(
            EXPENSES_URL, {"category": "other", "amount": "10"}, format="json"
        )

        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["expense_date"], "2026-01-01")

    def test_salary_cannot_be_entered_edited_or_voided_by_hand(self):
        self.monthly()
        self.pay(self.ram, "5000.00")
        expense_id = Expense.objects.get().pk

        self.assertEqual(self.expense(category="salary").status_code, 400)
        patched = self.client.patch(
            f"{EXPENSES_URL}{expense_id}/", {"amount": "1"}, format="json"
        )
        voided = self.client.post(
            f"{EXPENSES_URL}{expense_id}/void/", {"reason": "x"}, format="json"
        )

        self.assertEqual(patched.status_code, 409)
        self.assertEqual(patched.data["code"], "payment_expense_locked")
        self.assertEqual(voided.status_code, 409)

    def test_expense_rules(self):
        self.assertEqual(self.expense(amount="0").status_code, 400)
        self.assertEqual(self.expense(category="fun").status_code, 400)
        self.assertEqual(self.expense(expense_date="2026-01-02").status_code, 400)

    def test_summary_totals_active_expenses_by_category(self):
        self.monthly()
        self.pay(self.ram, "5000.00")
        self.expense(amount="15000.00")
        wrong = self.expense(amount="999.00").data["id"]
        self.client.post(f"{EXPENSES_URL}{wrong}/void/", {"reason": "x"}, format="json")

        response = self.client.get(f"{EXPENSES_URL}summary/")

        totals = {r["category"]: money(r["total"]) for r in response.data["by_category"]}
        self.assertEqual(totals, {"rent": money("15000"), "salary": money("5000")})
        self.assertEqual(money(response.data["total"]), money("20000"))
        self.assertEqual(response.json()["total"], "20000.00")

    def test_expenses_are_scoped_to_the_business(self):
        self.expense()

        self.authenticate("OTHER-HO", "owner")

        self.assertEqual(self.client.get(EXPENSES_URL).data["count"], 0)

        self.authenticate("ACME-BR", "owner")

        self.assertEqual(self.client.get(EXPENSES_URL).data["count"], 0)
        self.assertEqual(self.expense().status_code, 403)


class PayrollModelTests(PayrollTestCase):

    def test_database_refuses_a_second_live_wage_for_a_day(self):
        fields = dict(
            business=self.ho, staff=self.sita, kind="wage",
            entry_date=date(2026, 1, 1), amount=Decimal("100"),
        )
        PayrollEntry.objects.create(**fields)

        with self.assertRaises(IntegrityError), transaction.atomic():
            PayrollEntry.objects.create(**fields)

        PayrollEntry.objects.update(status="void")
        PayrollEntry.objects.create(**fields)

    def test_database_refuses_non_positive_amounts(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            PayrollEntry.objects.create(
                business=self.ho, staff=self.sita, kind="bonus",
                entry_date=date(2026, 1, 1), amount=Decimal("0"),
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            PayrollPayment.objects.create(
                business=self.ho, staff=self.sita, amount=Decimal("-1"),
                paid_on=date(2026, 1, 1), method="cash",
            )

    def test_database_keeps_the_rate_in_line_with_the_pay_type(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            SalaryProfile.objects.create(
                user=self.sita, pay_type="daily", rate=Decimal("100"),
                start_date=date(2026, 1, 1), accrued_through=date(2025, 12, 31),
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            SalaryProfile.objects.create(
                user=self.sita, pay_type="monthly", rate=None,
                start_date=date(2026, 1, 1), accrued_through=date(2025, 12, 31),
            )


class StatementTests(PayrollTestCase):

    def statement(self, staff, **query):
        response = self.client.get(
            f"{BALANCES_URL}{staff.pk}/statement/", query
        )
        self.assertEqual(response.status_code, 200, response.data)

        return response.json()

    def test_an_advance_shows_how_much_was_paid_when_and_what_is_left(self):
        self.monthly(day=5)
        self.today(date(2026, 1, 2))

        paid = self.pay(self.ram, "10000.00")

        # The response already says where the account stands.
        self.assertEqual(
            paid.json()["balance_after"], {"remaining": "0.00", "advance": "10000.00"}
        )

        line = self.statement(self.ram)["results"][0]
        self.assertEqual(line["type"], "payment")
        self.assertEqual(line["date"], "2026-01-02")
        self.assertEqual(line["amount"], "10000.00")
        self.assertEqual(line["method"], "cash")
        self.assertEqual(line["advance_part"], "10000.00")
        self.assertEqual(line["advance_after"], "10000.00")
        self.assertEqual(line["owed_after"], "0.00")

        # On the salary day the advance is used up and the rest is owed.
        self.today(date(2026, 1, 5))
        lines = self.statement(self.ram)["results"]

        self.assertEqual([l["type"] for l in lines], ["salary", "payment"])
        self.assertEqual(lines[0]["amount"], "32000.00")
        self.assertEqual(lines[0]["owed_after"], "22000.00")
        self.assertEqual(lines[0]["advance_after"], "0.00")

    def test_a_payment_after_the_salary_is_not_an_advance(self):
        self.monthly(day=5)
        self.today(date(2026, 1, 5))

        paid = self.pay(self.ram, "12000.00")
        line = self.statement(self.ram)["results"][0]

        self.assertEqual(paid.json()["balance_after"]["remaining"], "20000.00")
        self.assertEqual(line["advance_part"], "0.00")
        self.assertEqual(line["owed_after"], "20000.00")

    def test_paying_more_than_owed_splits_the_advance_part(self):
        self.monthly(day=5)
        self.today(date(2026, 1, 5))

        self.pay(self.ram, "40000.00")
        line = self.statement(self.ram)["results"][0]

        self.assertEqual(line["advance_part"], "8000.00")
        self.assertEqual(line["advance_after"], "8000.00")

    def test_daily_statement_runs_day_by_day(self):
        self.daily()
        self.today(date(2026, 1, 10))
        self.wage(self.sita, "2026-01-09", "800.00")
        self.pay(self.sita, "500.00")
        self.wage(self.sita, "2026-01-10", "900.00")

        results = self.statement(self.sita)["results"]

        # Newest first.
        self.assertEqual(
            [(r["date"], r["type"], r["owed_after"]) for r in results],
            [
                # Within a day, earnings come before payments.
                ("2026-01-10", "payment", "1200.00"),
                ("2026-01-10", "wage", "1700.00"),
                ("2026-01-09", "wage", "800.00"),
            ],
        )

    def test_deductions_and_voided_items(self):
        self.daily()
        self.today(date(2026, 1, 10))
        self.wage(self.sita, "2026-01-10", "800.00")
        self.entry(self.sita, "deduction", "2026-01-10", "100")
        payment_id = self.pay(self.sita, "300.00").data["id"]
        self.client.post(
            f"{PAYMENTS_URL}{payment_id}/void/", {"reason": "typo"}, format="json"
        )

        results = self.statement(self.sita)["results"]

        self.assertEqual([r["type"] for r in results], ["deduction", "wage"])
        self.assertEqual(results[0]["direction"], "deducted")
        self.assertEqual(results[0]["owed_after"], "700.00")

    def test_date_filter_keeps_the_running_figures(self):
        self.daily()
        self.today(date(2026, 1, 10))
        self.wage(self.sita, "2026-01-08", "800.00")
        self.wage(self.sita, "2026-01-10", "900.00")

        body = self.statement(self.sita, date_from="2026-01-10")

        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["owed_after"], "1700.00")

    def test_last_line_matches_the_balance(self):
        self.monthly(day=5)
        self.today(date(2026, 3, 20))
        self.pay(self.ram, "50000.00")

        newest = self.statement(self.ram)["results"][0]

        self.assertEqual(
            newest["owed_after"], self.balance(self.ram)["remaining"]
        )

    def test_statement_is_owner_only_and_scoped(self):
        self.monthly()

        self.authenticate("OTHER-HO", "owner")
        self.assertEqual(
            self.client.get(f"{BALANCES_URL}{self.ram.pk}/statement/").status_code, 404
        )

        self.authenticate("ACME-HO", "owner")
        # No pay set up for sita.
        self.assertEqual(
            self.client.get(f"{BALANCES_URL}{self.sita.pk}/statement/").status_code, 404
        )

        self.authenticate("ACME-HO", "ram", password=self.password)
        self.assertEqual(
            self.client.get(f"{BALANCES_URL}{self.ram.pk}/statement/").status_code, 403
        )


class MySalaryTrackerTests(PayrollTestCase):
    """Staff see their own pay, and nobody else's."""

    def sign_in(self, username, code="ACME-HO"):
        self.authenticate(code, username, password=self.password)

    def test_staff_see_their_own_account(self):
        self.monthly(day=5)
        self.today(date(2026, 1, 2))
        self.pay(self.ram, "10000.00")
        self.today(date(2026, 1, 5))

        self.sign_in("ram")
        mine = self.client.get("/api/v1/payroll/me/")

        self.assertEqual(mine.status_code, 200, mine.data)
        body = mine.json()
        self.assertEqual(body["staff"], self.ram.pk)
        self.assertEqual(body["pay_type"], "monthly")
        self.assertEqual(body["total_earned"], "32000.00")
        self.assertEqual(body["total_paid"], "10000.00")
        self.assertEqual(body["remaining"], "22000.00")
        self.assertEqual(body["next_salary_date"], "2026-02-05")
        self.assertEqual(body["recent_payments"][0]["amount"], "10000.00")
        self.assertEqual(body["recent_payments"][0]["paid_on"], "2026-01-02")

    def test_staff_see_their_own_statement_with_advance_and_balance(self):
        self.monthly(day=5)
        self.today(date(2026, 1, 2))
        self.pay(self.ram, "10000.00")

        self.sign_in("ram")
        lines = self.client.get("/api/v1/payroll/me/statement/").json()["results"]

        self.assertEqual(lines[0]["advance_part"], "10000.00")
        self.assertEqual(lines[0]["advance_after"], "10000.00")

    def test_a_daily_worker_sees_what_is_still_owed(self):
        self.daily()
        self.today(date(2026, 1, 10))
        self.wage(self.sita, "2026-01-10", "800.00")
        self.pay(self.sita, "500.00")

        self.sign_in("sita")
        body = self.client.get("/api/v1/payroll/me/").json()

        self.assertEqual(body["remaining"], "300.00")
        self.assertEqual(body["unsettled"][0]["state"], "partial")

    def test_staff_never_see_anyone_elses_pay(self):
        self.monthly()
        self.daily()
        self.pay(self.ram, "100.00")
        self.pay(self.sita, "999.00")

        self.sign_in("ram")

        statement = self.client.get("/api/v1/payroll/me/statement/").json()
        self.assertEqual({r["amount"] for r in statement["results"]}, {"100.00"})
        for url in (BALANCES_URL, PAYMENTS_URL, ENTRIES_URL, SALARIES_URL):
            self.assertEqual(self.client.get(url).status_code, 403)

    def test_people_without_pay_set_up_get_a_clear_404(self):
        self.sign_in("ram")

        response = self.client.get("/api/v1/payroll/me/")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "no_salary")
        self.assertEqual(
            self.client.get("/api/v1/payroll/me/statement/").status_code, 404
        )

    def test_it_requires_signing_in(self):
        self.client.credentials()

        self.assertEqual(self.client.get("/api/v1/payroll/me/").status_code, 401)

    def test_a_branch_staff_member_can_see_their_pay(self):
        self.make_user("bowner", User.Role.OWNER, business=self.branch)
        position = self.make_position(self.branch, "Branch Sales")
        bina = self.make_user(
            "bina", User.Role.STAFF, business=self.branch, position=position
        )
        self.authenticate("ACME-BR", "bowner")
        self.monthly(staff=bina, rate="18000.00", allowances="0.00", day=1)
        self.today(date(2026, 1, 3))
        self.pay(bina, "4000.00")

        self.sign_in("bina", "ACME-BR")
        body = self.client.get("/api/v1/payroll/me/").json()

        self.assertEqual(body["remaining"], "14000.00")
