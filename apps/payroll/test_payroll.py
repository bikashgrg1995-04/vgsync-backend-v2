from datetime import date, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.common.testing import TenantTestMixin
from apps.payroll.models import PayrollRecord, SalaryProfile

SALARIES_URL = "/api/v1/payroll/salaries/"
RECORDS_URL = "/api/v1/payroll/records/"

JAN = {"period_start": "2026-01-01", "period_end": "2026-01-31"}
FEB = {"period_start": "2026-02-01", "period_end": "2026-02-28"}


def money(value):
    return Decimal(value)


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

    def set_salary(self, staff, basic="30000.00", allowances="2000.00"):
        return self.client.put(
            f"{SALARIES_URL}{staff.pk}/",
            {"rate": basic, "allowances": allowances},
            format="json",
        )

    def create_record(self, staff=None, period=JAN, **extra):
        payload = {"staff": (staff or self.ram).pk, **period, **extra}

        return self.client.post(RECORDS_URL, payload, format="json")

    def pay(self, record_id, **extra):
        payload = {"payment_method": "cash", **extra}

        return self.client.post(f"{RECORDS_URL}{record_id}/pay/", payload, format="json")


class SalaryTests(PayrollTestCase):

    def test_set_then_change_a_salary(self):
        created = self.set_salary(self.ram)

        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["staff"], self.ram.pk)
        self.assertEqual(money(created.data["rate"]), money("30000.00"))
        self.assertEqual(created.data["staff_name"], "Ram Thapa")

        updated = self.set_salary(self.ram, basic="35000.00")

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(SalaryProfile.objects.filter(user=self.ram).count(), 1)
        self.assertEqual(
            SalaryProfile.objects.get(user=self.ram).updated_by, self.owner
        )

        fetched = self.client.get(f"{SALARIES_URL}{self.ram.pk}/")
        self.assertEqual(money(fetched.data["rate"]), money("35000.00"))

    def test_negative_or_missing_salary_is_rejected(self):
        negative = self.set_salary(self.ram, basic="-1")
        missing = self.client.put(
            f"{SALARIES_URL}{self.ram.pk}/", {}, format="json"
        )

        self.assertEqual(negative.status_code, 400)
        self.assertEqual(missing.status_code, 400)
        self.assertFalse(SalaryProfile.objects.exists())

    def test_salary_list_is_paginated_and_scoped(self):
        self.set_salary(self.ram)
        self.set_salary(self.sita)

        response = self.client.get(SALARIES_URL)

        self.assertEqual(response.data["count"], 2)

        self.authenticate("OTHER-HO", "owner")
        self.assertEqual(self.client.get(SALARIES_URL).data["count"], 0)

    def test_cannot_touch_another_companys_staff_salary(self):
        self.set_salary(self.ram)
        self.authenticate("OTHER-HO", "owner")

        self.assertEqual(self.client.get(f"{SALARIES_URL}{self.ram.pk}/").status_code, 404)
        self.assertEqual(self.set_salary(self.ram, basic="1.00").status_code, 404)
        self.assertEqual(
            money(SalaryProfile.objects.get(user=self.ram).rate),
            money("30000.00"),
        )

    def test_the_owner_has_no_salary_record_here(self):
        self.assertEqual(self.set_salary(self.owner).status_code, 404)

    def test_no_salary_set_returns_404(self):
        self.assertEqual(self.client.get(f"{SALARIES_URL}{self.ram.pk}/").status_code, 404)


class PayrollRecordTests(PayrollTestCase):

    def test_record_defaults_to_the_salary_and_the_server_calculates_net(self):
        self.set_salary(self.ram, basic="30000.00", allowances="2000.00")

        response = self.create_record(
            bonus="1500.00", deductions="500.00", advance_deduction="1000.00",
            net_salary="1.00",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(money(response.data["basic_salary"]), money("30000.00"))
        self.assertEqual(money(response.data["allowances"]), money("2000.00"))
        # 30000 + 2000 + 1500 - 500 - 1000; the client's net_salary is ignored.
        self.assertEqual(money(response.data["net_salary"]), money("32000.00"))
        self.assertEqual(response.data["status"], "draft")

        record = PayrollRecord.objects.get(pk=response.data["id"])
        self.assertEqual(record.business, self.ho)
        self.assertEqual(record.created_by, self.owner)

    def test_a_later_salary_change_does_not_alter_existing_records(self):
        self.set_salary(self.ram, basic="30000.00")
        record_id = self.create_record().data["id"]

        self.set_salary(self.ram, basic="99999.00")

        self.assertEqual(
            money(self.client.get(f"{RECORDS_URL}{record_id}/").data["basic_salary"]),
            money("30000.00"),
        )

    def test_without_a_salary_the_rate_must_be_given(self):
        missing = self.create_record()
        explicit = self.create_record(rate="20000.00")

        self.assertEqual(missing.status_code, 400)
        self.assertIn("rate", missing.data)
        self.assertEqual(explicit.status_code, 201, explicit.data)
        self.assertEqual(money(explicit.data["net_salary"]), money("20000.00"))

    def test_deductions_cannot_exceed_the_pay(self):
        response = self.create_record(
            rate="1000.00", deductions="900.00", advance_deduction="200.00"
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PayrollRecord.objects.exists())

    def test_negative_amounts_are_rejected(self):
        response = self.create_record(rate="1000.00", bonus="-5")

        self.assertEqual(response.status_code, 400)
        self.assertIn("bonus", response.data)

    def test_period_end_cannot_precede_start(self):
        response = self.create_record(
            period={"period_start": "2026-02-10", "period_end": "2026-02-01"},
            rate="1000.00",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("period_end", response.data)

    def test_overlapping_periods_are_rejected(self):
        self.assertEqual(self.create_record(rate="1000").status_code, 201)

        same = self.create_record(rate="1000")
        partial = self.create_record(
            period={"period_start": "2026-01-15", "period_end": "2026-02-14"},
            rate="1000",
        )
        next_month = self.create_record(period=FEB, rate="1000")

        self.assertEqual(same.status_code, 400)
        self.assertEqual(partial.status_code, 400)
        self.assertEqual(next_month.status_code, 201, next_month.data)

    def test_inactive_staff_cannot_be_paid(self):
        User.objects.filter(pk=self.ram.pk).update(is_active=False)

        response = self.create_record(rate="1000")

        self.assertEqual(response.status_code, 400)
        self.assertIn("staff", response.data)

    def test_edit_a_draft_recalculates_net(self):
        record_id = self.create_record(rate="1000.00").data["id"]

        response = self.client.patch(
            f"{RECORDS_URL}{record_id}/", {"bonus": "250.00"}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(money(response.data["net_salary"]), money("1250.00"))

    def test_person_and_period_of_a_draft_are_fixed(self):
        record_id = self.create_record(rate="1000.00").data["id"]

        for change in ({"staff": self.sita.pk}, {"period_end": "2026-03-31"}):
            response = self.client.patch(
                f"{RECORDS_URL}{record_id}/", change, format="json"
            )

            self.assertEqual(response.status_code, 400, change)

    def test_client_cannot_set_status_or_payment_fields(self):
        record_id = self.create_record(rate="1000.00").data["id"]

        self.client.patch(
            f"{RECORDS_URL}{record_id}/",
            {"status": "paid", "paid_on": "2026-01-31", "paid_by": self.owner.pk},
            format="json",
        )

        record = PayrollRecord.objects.get(pk=record_id)
        self.assertEqual(record.status, "draft")
        self.assertIsNone(record.paid_on)
        self.assertIsNone(record.paid_by)

    def test_delete_removes_a_draft(self):
        record_id = self.create_record(rate="1000.00").data["id"]

        self.assertEqual(self.client.delete(f"{RECORDS_URL}{record_id}/").status_code, 204)
        self.assertFalse(PayrollRecord.objects.exists())

    def test_list_filters_search_and_pagination(self):
        first = self.create_record(rate="1000.00").data["id"]
        self.create_record(staff=self.sita, period=FEB, rate="2000.00")
        self.pay(first)

        everything = self.client.get(RECORDS_URL)
        self.assertEqual(everything.data["count"], 2)
        self.assertEqual(
            sorted(everything.data), ["count", "next", "previous", "results"]
        )

        paid = self.client.get(RECORDS_URL, {"status": "paid"})
        self.assertEqual([r["id"] for r in paid.data["results"]], [first])

        by_staff = self.client.get(RECORDS_URL, {"staff": self.sita.pk})
        self.assertEqual(by_staff.data["count"], 1)

        by_period = self.client.get(RECORDS_URL, {"period_start__gte": "2026-02-01"})
        self.assertEqual(by_period.data["count"], 1)

        searched = self.client.get(RECORDS_URL, {"search": "thapa"})
        self.assertEqual(searched.data["count"], 1)


class PayAndVoidTests(PayrollTestCase):

    def setUp(self):
        super().setUp()

        self.record_id = self.create_record(rate="10000.00").data["id"]

    def test_pay_locks_the_record_and_records_who_and_how(self):
        response = self.pay(self.record_id, payment_reference="TXN-1")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "paid")
        self.assertEqual(response.data["paid_on"], str(timezone.localdate()))
        self.assertEqual(response.data["payment_method"], "cash")
        self.assertEqual(response.data["payment_reference"], "TXN-1")
        self.assertEqual(response.data["paid_by"], self.owner.pk)

    def test_pay_on_a_given_past_date(self):
        response = self.pay(self.record_id, paid_on="2026-02-01")

        self.assertEqual(response.data["paid_on"], "2026-02-01")

    def test_payment_date_cannot_be_in_the_future(self):
        tomorrow = timezone.localdate() + timedelta(days=1)

        response = self.pay(self.record_id, paid_on=str(tomorrow))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(PayrollRecord.objects.get(pk=self.record_id).status, "draft")

    def test_payment_method_is_required_and_validated(self):
        missing = self.client.post(f"{RECORDS_URL}{self.record_id}/pay/", {}, format="json")
        invalid = self.pay(self.record_id, payment_method="barter")

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(invalid.status_code, 400)

    def test_paying_twice_is_a_conflict_and_does_not_change_the_record(self):
        self.pay(self.record_id, payment_reference="FIRST")

        again = self.pay(self.record_id, payment_method="bank", payment_reference="SECOND")

        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.data["code"], "invalid_status")

        record = PayrollRecord.objects.get(pk=self.record_id)
        self.assertEqual(record.payment_method, "cash")
        self.assertEqual(record.payment_reference, "FIRST")

    def test_a_paid_record_cannot_be_edited_or_deleted(self):
        self.pay(self.record_id)

        edit = self.client.patch(
            f"{RECORDS_URL}{self.record_id}/", {"bonus": "5"}, format="json"
        )
        delete = self.client.delete(f"{RECORDS_URL}{self.record_id}/")

        self.assertEqual(edit.status_code, 409)
        self.assertEqual(edit.data["code"], "document_not_editable")
        self.assertEqual(delete.status_code, 409)
        self.assertTrue(PayrollRecord.objects.filter(pk=self.record_id).exists())

    def test_void_needs_a_reason_and_keeps_the_history(self):
        self.pay(self.record_id)

        blank = self.client.post(f"{RECORDS_URL}{self.record_id}/void/", {}, format="json")
        self.assertEqual(blank.status_code, 400)

        response = self.client.post(
            f"{RECORDS_URL}{self.record_id}/void/",
            {"reason": "Wrong amount"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "void")
        self.assertEqual(response.data["void_reason"], "Wrong amount")
        self.assertEqual(response.data["voided_by"], self.owner.pk)
        self.assertIsNotNone(response.data["voided_at"])
        self.assertEqual(response.data["payment_reference"], "")
        self.assertEqual(response.data["paid_by"], self.owner.pk)
        self.assertTrue(PayrollRecord.objects.filter(pk=self.record_id).exists())

    def test_a_draft_cannot_be_voided_and_a_void_cannot_be_voided_again(self):
        draft = self.client.post(
            f"{RECORDS_URL}{self.record_id}/void/", {"reason": "x"}, format="json"
        )
        self.assertEqual(draft.status_code, 409)

        self.pay(self.record_id)
        self.client.post(
            f"{RECORDS_URL}{self.record_id}/void/", {"reason": "x"}, format="json"
        )
        again = self.client.post(
            f"{RECORDS_URL}{self.record_id}/void/", {"reason": "y"}, format="json"
        )
        self.assertEqual(again.status_code, 409)

    def test_a_voided_period_can_be_paid_again(self):
        self.pay(self.record_id)
        self.client.post(
            f"{RECORDS_URL}{self.record_id}/void/", {"reason": "Wrong"}, format="json"
        )

        replacement = self.create_record(rate="10500.00")

        self.assertEqual(replacement.status_code, 201, replacement.data)
        self.assertEqual(PayrollRecord.objects.filter(staff=self.ram).count(), 2)


class GenerateTests(PayrollTestCase):

    def generate(self, period=JAN):
        return self.client.post(f"{RECORDS_URL}generate/", period, format="json")

    def test_generates_a_draft_for_each_person_with_a_salary(self):
        self.set_salary(self.ram, basic="30000.00", allowances="2000.00")
        self.set_salary(self.sita, basic="25000.00", allowances="0.00")

        response = self.generate()

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(response.data["created"]), 2)
        self.assertEqual(response.data["skipped"], [])

        nets = sorted(money(r["net_salary"]) for r in response.data["created"])
        self.assertEqual(nets, [money("25000.00"), money("32000.00")])
        self.assertTrue(
            all(r["status"] == "draft" for r in response.data["created"])
        )

    def test_people_without_a_salary_or_with_existing_payroll_are_skipped(self):
        self.set_salary(self.ram)
        self.generate()

        no_salary_and_existing = self.generate()

        reasons = {
            item["staff"]: item["reason"]
            for item in no_salary_and_existing.data["skipped"]
        }
        self.assertEqual(
            reasons,
            {self.ram.pk: "already_exists", self.sita.pk: "no_salary"},
        )
        self.assertEqual(no_salary_and_existing.data["created"], [])
        self.assertEqual(PayrollRecord.objects.count(), 1)

    def test_inactive_staff_are_left_out(self):
        self.set_salary(self.ram)
        self.set_salary(self.sita)
        User.objects.filter(pk=self.sita.pk).update(is_active=False)

        response = self.generate()

        self.assertEqual(
            [r["staff"] for r in response.data["created"]], [self.ram.pk]
        )

    def test_invalid_period_is_rejected(self):
        response = self.generate(
            {"period_start": "2026-02-10", "period_end": "2026-02-01"}
        )

        self.assertEqual(response.status_code, 400)


class PayrollAccessTests(PayrollTestCase):

    def test_only_the_owner_can_use_payroll(self):
        self.make_user("adm", User.Role.ADMIN, company=self.company)
        self.make_user("acc", User.Role.ACCOUNTANT, company=self.company)

        for username in ("adm", "acc", "ram"):
            self.authenticate("ACME-HO", username, password=self.password)

            for url in (SALARIES_URL, RECORDS_URL):
                self.assertEqual(self.client.get(url).status_code, 403, username)

            self.assertEqual(
                self.client.post(RECORDS_URL, {}, format="json").status_code,
                403,
                username,
            )

    def test_unauthenticated_requests_are_rejected(self):
        self.client.credentials()

        self.assertEqual(self.client.get(RECORDS_URL).status_code, 401)

    def test_another_companys_owner_sees_nothing_and_changes_nothing(self):
        self.set_salary(self.ram)
        record_id = self.create_record().data["id"]

        self.authenticate("OTHER-HO", "owner")

        self.assertEqual(self.client.get(RECORDS_URL).data["count"], 0)
        self.assertEqual(self.client.get(f"{RECORDS_URL}{record_id}/").status_code, 404)
        self.assertEqual(self.pay(record_id).status_code, 404)
        self.assertEqual(
            self.client.post(
                f"{RECORDS_URL}{record_id}/void/", {"reason": "x"}, format="json"
            ).status_code,
            404,
        )
        self.assertEqual(self.create_record().status_code, 400)

    def test_company_owner_is_read_only_in_a_branch(self):
        self.authenticate("ACME-BR", "owner")

        self.assertEqual(self.client.get(RECORDS_URL).status_code, 200)
        self.assertEqual(
            self.client.post(f"{RECORDS_URL}generate/", JAN, format="json").status_code,
            403,
        )
        self.assertEqual(self.set_salary(self.ram).status_code, 403)

    def test_branch_owner_runs_payroll_for_the_branch_team(self):
        self.make_user("bowner", User.Role.OWNER, business=self.branch)
        position = self.make_position(self.branch, "Branch Sales")
        bina = self.make_user(
            "bina", User.Role.STAFF, business=self.branch, position=position
        )
        self.authenticate("ACME-BR", "bowner")

        self.set_salary(bina, basic="18000.00", allowances="0.00")
        generated = self.client.post(f"{RECORDS_URL}generate/", JAN, format="json")

        self.assertEqual(generated.status_code, 201, generated.data)
        self.assertEqual([r["staff"] for r in generated.data["created"]], [bina.pk])
        self.assertEqual(PayrollRecord.objects.get().business, self.branch)

        # Company staff are not part of the branch team.
        self.assertEqual(self.set_salary(self.ram).status_code, 404)


class PayrollModelTests(PayrollTestCase):

    def test_net_salary_is_recalculated_on_every_save(self):
        record = PayrollRecord.objects.create(
            business=self.ho,
            staff=self.ram,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            rate=Decimal("1000.00"),
            bonus=Decimal("100.00"),
            net_salary=Decimal("999999.00"),
        )

        self.assertEqual(record.net_salary, Decimal("1100.00"))

        record.deductions = Decimal("300.00")
        record.save(update_fields=["deductions"])
        record.refresh_from_db()

        self.assertEqual(record.net_salary, Decimal("800.00"))

    def test_database_refuses_a_second_live_record_for_the_same_period(self):
        fields = dict(
            business=self.ho,
            staff=self.ram,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            rate=Decimal("1000.00"),
        )
        PayrollRecord.objects.create(**fields)

        with self.assertRaises(IntegrityError), transaction.atomic():
            PayrollRecord.objects.create(**fields)

        PayrollRecord.objects.filter(staff=self.ram).update(status="void")
        PayrollRecord.objects.create(**fields)

    def test_database_refuses_negative_net_pay(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            PayrollRecord.objects.create(
                business=self.ho,
                staff=self.ram,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
                rate=Decimal("100.00"),
                deductions=Decimal("500.00"),
            )


class PayTypeTests(PayrollTestCase):
    """Monthly, daily and hourly pay."""

    def set_pay(self, staff, pay_type, rate, allowances="0.00"):
        return self.client.put(
            f"{SALARIES_URL}{staff.pk}/",
            {"pay_type": pay_type, "rate": rate, "allowances": allowances},
            format="json",
        )

    def test_pay_type_defaults_to_monthly(self):
        response = self.set_salary(self.ram)

        self.assertEqual(response.data["pay_type"], "monthly")

    def test_a_pay_type_must_be_a_known_one(self):
        self.assertEqual(self.set_pay(self.ram, "weekly", "100").status_code, 400)

    def test_daily_pay_is_rate_times_days_worked(self):
        self.set_pay(self.ram, "daily", "800.00", allowances="500.00")

        response = self.create_record(units="22", bonus="100.00")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["pay_type"], "daily")
        self.assertEqual(money(response.data["rate"]), money("800.00"))
        self.assertEqual(money(response.data["units"]), money("22.00"))
        self.assertEqual(money(response.data["basic_salary"]), money("17600.00"))
        # 17600 + 500 + 100
        self.assertEqual(money(response.data["net_salary"]), money("18200.00"))

    def test_hourly_pay_allows_part_hours(self):
        self.set_pay(self.ram, "hourly", "150.00")

        response = self.create_record(units="160.50")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(money(response.data["net_salary"]), money("24075.00"))

    def test_units_can_be_filled_in_later_on_the_draft(self):
        self.set_pay(self.ram, "hourly", "100.00")
        record_id = self.create_record().data["id"]

        self.assertEqual(
            money(self.client.get(f"{RECORDS_URL}{record_id}/").data["net_salary"]),
            money("0.00"),
        )

        edited = self.client.patch(
            f"{RECORDS_URL}{record_id}/", {"units": "40"}, format="json"
        )

        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertEqual(money(edited.data["net_salary"]), money("4000.00"))

    def test_monthly_pay_rejects_units(self):
        self.set_salary(self.ram)

        response = self.create_record(units="10")

        self.assertEqual(response.status_code, 400)
        self.assertIn("units", response.data)

    def test_units_cannot_exceed_the_period(self):
        self.set_pay(self.ram, "daily", "800.00")
        self.set_pay(self.sita, "hourly", "100.00")

        self.assertEqual(self.create_record(units="32").status_code, 400)
        self.assertEqual(self.create_record(units="31").status_code, 201)
        self.assertEqual(
            self.create_record(staff=self.sita, units="745").status_code, 400
        )
        self.assertEqual(
            self.create_record(staff=self.sita, units="744").status_code, 201
        )

    def test_negative_units_are_rejected(self):
        self.set_pay(self.ram, "daily", "800.00")

        self.assertEqual(self.create_record(units="-1").status_code, 400)

    def test_a_different_pay_type_needs_its_own_rate(self):
        self.set_pay(self.ram, "monthly", "30000.00")

        reused = self.create_record(pay_type="hourly", units="10")
        explicit = self.create_record(pay_type="hourly", rate="200", units="10")

        self.assertEqual(reused.status_code, 400)
        self.assertEqual(explicit.status_code, 201, explicit.data)
        self.assertEqual(money(explicit.data["basic_salary"]), money("2000.00"))

    def test_basic_salary_sent_by_the_client_is_ignored(self):
        self.set_pay(self.ram, "daily", "800.00")

        response = self.create_record(units="10", basic_salary="999999.00")

        self.assertEqual(money(response.data["basic_salary"]), money("8000.00"))

    def test_a_later_rate_change_does_not_alter_existing_records(self):
        self.set_pay(self.ram, "daily", "800.00")
        record_id = self.create_record(units="10").data["id"]

        self.set_pay(self.ram, "hourly", "1.00")

        record = self.client.get(f"{RECORDS_URL}{record_id}/").data
        self.assertEqual(record["pay_type"], "daily")
        self.assertEqual(money(record["basic_salary"]), money("8000.00"))

    def test_generate_makes_each_person_a_draft_of_their_own_pay_type(self):
        self.set_salary(self.ram)
        self.set_pay(self.sita, "daily", "800.00", allowances="300.00")

        response = self.client.post(
            f"{RECORDS_URL}generate/", JAN, format="json"
        )

        self.assertEqual(response.status_code, 201, response.data)

        by_staff = {r["staff"]: r for r in response.data["created"]}
        self.assertEqual(by_staff[self.ram.pk]["pay_type"], "monthly")
        self.assertIsNone(by_staff[self.ram.pk]["units"])
        self.assertEqual(by_staff[self.sita.pk]["pay_type"], "daily")
        self.assertEqual(money(by_staff[self.sita.pk]["units"]), money("0"))
        # Waiting for the days worked: only the allowance so far.
        self.assertEqual(money(by_staff[self.sita.pk]["net_salary"]), money("300.00"))

    def test_a_daily_or_hourly_draft_cannot_be_paid_without_units(self):
        self.set_pay(self.ram, "daily", "800.00", allowances="300.00")
        record_id = self.create_record().data["id"]

        blocked = self.pay(record_id)

        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.data["code"], "units_required")

        self.client.patch(f"{RECORDS_URL}{record_id}/", {"units": "5"}, format="json")

        self.assertEqual(self.pay(record_id).status_code, 200)

    def test_switching_a_draft_to_monthly_drops_the_units(self):
        self.set_pay(self.ram, "daily", "800.00")
        record_id = self.create_record(units="5").data["id"]

        edited = self.client.patch(
            f"{RECORDS_URL}{record_id}/",
            {"pay_type": "monthly", "rate": "20000.00"},
            format="json",
        )

        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertIsNone(edited.data["units"])
        self.assertEqual(money(edited.data["net_salary"]), money("20000.00"))

    def test_records_can_be_filtered_by_pay_type(self):
        self.set_salary(self.ram)
        self.set_pay(self.sita, "hourly", "100.00")
        self.create_record()
        self.create_record(staff=self.sita, units="10")

        response = self.client.get(RECORDS_URL, {"pay_type": "hourly"})

        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["staff"], self.sita.pk)

    def test_database_refuses_units_on_monthly_pay(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            PayrollRecord.objects.create(
                business=self.ho,
                staff=self.ram,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
                pay_type="monthly",
                rate=Decimal("1000.00"),
                units=Decimal("5"),
            )
