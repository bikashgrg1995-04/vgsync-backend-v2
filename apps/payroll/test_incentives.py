from datetime import date
from decimal import Decimal

from apps.accounts import rbac
from apps.common.testing import User
from apps.payroll.models import PayrollEntry, SalaryProfile
from apps.sales.test_bike_maintenance import BASE, BikeTestCase

RULES = "/api/v1/payroll/incentive-rules/"
INCENTIVES = "/api/v1/payroll/incentives/"
MINE = "/api/v1/payroll/me/incentive/"


class IncentiveTests(BikeTestCase):

    def setUp(self):
        super().setUp()
        pos = self.make_position(self.ho, "Seller")
        rbac.write_position_codes(pos, rbac.preset_codes("sales_staff"))
        self.ram = self.make_user("ram", User.Role.STAFF, company=self.company, position=pos)
        tech_pos = self.make_position(self.ho, "Technician", inventory=("view",))
        self.tech = self.make_user("tech", User.Role.STAFF, company=self.company, position=tech_pos)
        for user in (self.ram, self.tech):
            SalaryProfile.objects.create(
                user=user, pay_type="monthly", rate=Decimal("10000"),
                start_date=date(2026, 1, 1), accrued_through=date(2026, 10, 31),
            )
        self.month = date.today().strftime("%Y-%m")

    def rule(self, **extra):
        self.authenticate("ACME-HO", "owner")

        return self.client.post(
            RULES,
            {"name": "Sales commission", "basis": "sale_percent", "value": "5", **extra},
            format="json",
        )

    def sell(self, price="1000"):
        self.authenticate("ACME-HO", "ram")
        made = self.client.post(
            BASE,
            {
                "sale_type": "maintenance",
                "customer": self.customer.pk,
                "items": [{"description": "Service", "quantity": "1", "unit_price": price}],
            },
            format="json",
        ).json()
        self.client.post(f"{BASE}{made['id']}/confirm/", {}, format="json")

        return made

    def preview(self):
        self.authenticate("ACME-HO", "owner")

        return self.client.get(INCENTIVES, {"month": self.month}).json()["rows"]

    def test_a_percent_rule_pays_on_what_the_person_sold(self):
        self.assertEqual(self.rule().status_code, 201)
        self.sell("1000")
        self.sell("2000")

        row = next(r for r in self.preview() if r["staff"] == self.ram.pk)

        self.assertEqual(row["total"], "150.00")
        self.assertEqual(row["lines"][0]["base"], "3000.00")

    def test_a_rule_for_one_position_only_counts_that_position(self):
        self.rule(position=self.ram.position_id)
        self.sell("1000")
        rows = self.preview()

        self.assertEqual([r["staff"] for r in rows], [self.ram.pk])

    def test_applying_makes_one_bonus_and_cannot_repeat(self):
        self.rule()
        self.sell("1000")
        self.authenticate("ACME-HO", "owner")

        first = self.client.post(INCENTIVES, {"month": self.month}, format="json")
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(len(first.json()["applied"]), 1)

        entry = PayrollEntry.objects.get(staff=self.ram, incentive_month__isnull=False)
        self.assertEqual(entry.amount, Decimal("50.00"))
        self.assertEqual(entry.kind, "bonus")

        again = self.client.post(INCENTIVES, {"month": self.month}, format="json").json()
        self.assertEqual(again["applied"], [])
        self.assertEqual(again["skipped"][0]["reason"], "already_applied")

    def test_after_voiding_it_can_be_applied_again(self):
        self.rule()
        self.sell("1000")
        self.authenticate("ACME-HO", "owner")
        self.client.post(INCENTIVES, {"month": self.month}, format="json")
        entry = PayrollEntry.objects.get(staff=self.ram, incentive_month__isnull=False)
        self.client.post(f"/api/v1/payroll/entries/{entry.pk}/void/", {"reason": "redo"}, format="json")

        again = self.client.post(INCENTIVES, {"month": self.month}, format="json").json()

        self.assertEqual(len(again["applied"]), 1)

    def test_someone_without_a_pay_profile_is_skipped(self):
        SalaryProfile.objects.filter(user=self.ram).delete()
        self.rule()
        self.sell("1000")
        self.authenticate("ACME-HO", "owner")

        done = self.client.post(INCENTIVES, {"month": self.month}, format="json").json()

        self.assertEqual(done["applied"], [])
        self.assertEqual(done["skipped"][0]["reason"], "no_pay_profile")

    def test_a_future_month_cannot_be_applied(self):
        self.authenticate("ACME-HO", "owner")

        response = self.client.post(INCENTIVES, {"month": "2099-01"}, format="json")

        self.assertEqual(response.status_code, 409)

    def test_rules_are_owner_only_to_change(self):
        acc_pos = self.make_position(self.ho, "Acc")
        rbac.write_position_codes(acc_pos, rbac.preset_codes("accountant"))
        self.make_user("acc", User.Role.STAFF, company=self.company, position=acc_pos)
        self.authenticate("ACME-HO", "acc")

        self.assertEqual(self.client.get(RULES).status_code, 403)
        self.assertEqual(
            self.client.post(RULES, {"name": "x", "basis": "bike_fixed", "value": "10"}, format="json").status_code,
            403,
        )

    def test_percent_is_capped_and_both_targets_refused(self):
        self.assertEqual(self.rule(value="150").status_code, 400)
        self.assertEqual(
            self.rule(staff=self.ram.pk, position=self.ram.position_id).status_code, 400
        )

    def test_a_person_sees_their_own_estimate(self):
        self.rule()
        self.sell("2000")
        self.authenticate("ACME-HO", "ram")

        mine = self.client.get(MINE).json()

        self.assertEqual(mine["total"], "100.00")
        self.authenticate("ACME-HO", "tech")
        self.assertEqual(self.client.get(MINE).json()["total"], "0.00")

    def test_job_rules_pay_the_technician_on_finished_jobs(self):
        self.rule(name="Per job", basis="job_fixed", value="200", position=self.tech.position_id)
        self.rule(name="Job share", basis="job_percent", value="10", position=self.tech.position_id)
        self.authenticate("ACME-HO", "owner")
        job = self.client.post(
            BASE,
            {
                "sale_type": "maintenance", "customer": self.customer.pk,
                "technician": self.tech.pk,
                "items": [{"description": "Overhaul", "quantity": "1", "unit_price": "1500"}],
            },
            format="json",
        ).json()
        self.client.post(f"{BASE}{job['id']}/job-start/")
        self.client.post(f"{BASE}{job['id']}/job-done/", {}, format="json")
        self.client.post(f"{BASE}{job['id']}/confirm/", {}, format="json")

        row = next(r for r in self.preview() if r["staff"] == self.tech.pk)

        self.assertEqual(row["total"], "350.00")  # 200 + 10% of 1500
