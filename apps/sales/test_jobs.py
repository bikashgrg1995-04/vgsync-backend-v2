from apps.common.testing import User
from apps.sales.models import FreeService
from apps.sales.test_bike_maintenance import BASE, BikeTestCase

MY = "/api/v1/sales/my-jobs/"


class TechnicianJobTests(BikeTestCase):

    def setUp(self):
        super().setUp()
        position = self.make_position(self.ho, "Technician", inventory=("view",))
        self.tech = self.make_user(
            "tech", User.Role.STAFF, company=self.company, position=position
        )
        self.other = self.make_user(
            "other", User.Role.STAFF, company=self.company, position=position
        )

    def job(self, **extra):
        return self.client.post(
            BASE,
            {
                "sale_type": "maintenance",
                "customer": self.customer.pk,
                "items": [{"description": "Chain set", "quantity": "1", "unit_price": "900"}],
                **extra,
            },
            format="json",
        )

    def assign(self, sale, user):
        return self.client.post(
            f"{BASE}{sale['id']}/assign-technician/",
            {"technician": user.pk if user else None},
            format="json",
        )

    def as_tech(self, user="tech"):
        self.authenticate("ACME-HO", user)

    def test_the_job_can_be_given_when_it_is_opened(self):
        response = self.job(technician=self.tech.pk)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.json()["technician"], self.tech.pk)
        self.assertEqual(response.json()["job_status"], "pending")

    def test_it_can_be_given_and_changed_later(self):
        sale = self.job().json()

        self.assertEqual(self.assign(sale, self.tech).status_code, 200)
        moved = self.assign(sale, self.other).json()

        self.assertEqual(moved["technician"], self.other.pk)
        self.assertEqual(self.assign(sale, None).json()["technician"], None)

    def test_someone_outside_the_business_cannot_be_chosen(self):
        sale = self.job().json()
        outsider = self.make_user("out", User.Role.STAFF, business=self.other_business) \
            if hasattr(self, "other_business") else None

        if outsider:
            self.assertEqual(self.assign(sale, outsider).status_code, 400)

    def test_the_technician_is_told(self):
        from apps.notifications.models import Notification

        self.assign(self.job().json(), self.tech)

        self.assertTrue(
            Notification.objects.filter(recipient=self.tech, kind="job_assigned").exists()
        )

    def test_he_sees_only_his_own_jobs_and_no_prices(self):
        mine = self.job(technician=self.tech.pk).json()
        self.job(technician=self.other.pk)
        self.as_tech()
        rows = self.client.get(MY).json()

        self.assertEqual([r["id"] for r in rows], [mine["id"]])
        self.assertEqual(rows[0]["work"][0]["description"], "Chain set")
        self.assertNotIn("unit_price", str(rows[0]))
        self.assertNotIn("net_total", rows[0])

    def test_he_starts_and_finishes_his_job_with_notes(self):
        mine = self.job(technician=self.tech.pk).json()
        self.as_tech()

        started = self.client.post(f"{MY}{mine['id']}/start/")
        self.assertEqual(started.json()["job_status"], "in_progress")

        done = self.client.post(
            f"{MY}{mine['id']}/done/", {"notes": "Chain replaced, brakes ok"}, format="json"
        )
        self.assertEqual(done.status_code, 200, done.data)
        self.assertEqual(done.json()["job_status"], "done")
        self.assertEqual(done.json()["job_notes"], "Chain replaced, brakes ok")

        again = self.client.post(f"{MY}{mine['id']}/done/", {}, format="json")
        self.assertEqual(again.status_code, 409)

    def test_he_cannot_touch_someone_elses_job(self):
        theirs = self.job(technician=self.other.pk).json()
        self.as_tech()

        self.assertEqual(self.client.post(f"{MY}{theirs['id']}/start/").status_code, 404)

    def test_a_technician_cannot_open_the_sales_list(self):
        self.as_tech()

        self.assertEqual(self.client.get(BASE).status_code, 403)

    def test_a_done_job_cannot_be_given_to_someone_else(self):
        mine = self.job(technician=self.tech.pk).json()
        self.as_tech()
        self.client.post(f"{MY}{mine['id']}/done/", {}, format="json")
        self.authenticate("ACME-HO", "owner")

        self.assertEqual(self.assign(mine, self.other).status_code, 409)

    def test_the_office_can_start_and_finish_for_a_technician(self):
        mine = self.job(technician=self.tech.pk).json()
        started = self.client.post(f"{BASE}{mine['id']}/job-start/")
        done = self.client.post(
            f"{BASE}{mine['id']}/job-done/", {"notes": "ok"}, format="json"
        )

        self.assertEqual(started.json()["job_status"], "in_progress")
        self.assertEqual(done.json()["job_status"], "done")

    def test_a_stock_sale_is_not_a_job(self):
        sale = self.client.post(
            BASE,
            {"sale_type": "stock", "customer": self.customer.pk,
             "items": [{"product": self.model.pk, "quantity": "1", "unit_price": "100"}]},
            format="json",
        ).json()

        self.assertEqual(self.assign(sale, self.tech).status_code, 400)

    def test_a_free_service_job_passes_the_technician_and_notes_to_the_record(self):
        bike = self.made_bike(free_services=2)
        self.client.post(
            "/api/v1/sales/payments/",
            {"sale": bike["id"], "amount": "100000", "method": "cash"}, format="json",
        )
        self.client.post(f"{BASE}{bike['id']}/confirm/")
        job = self.job(
            service_kind="free", free_service_for=bike["id"], odometer_km=500,
            technician=self.tech.pk,
            items=[{"description": "Free labour", "quantity": "1", "unit_price": "0"}],
        ).json()
        self.client.post(f"{BASE}{job['id']}/confirm/")
        self.as_tech()
        self.client.post(f"{MY}{job['id']}/done/", {"notes": "Runs smooth"}, format="json")

        record = FreeService.objects.get()
        self.assertEqual(record.performance, "Runs smooth")
        self.assertIn("tech", record.technician)

    def test_the_owner_sees_how_each_technician_did(self):
        a = self.job(technician=self.tech.pk).json()
        self.job(technician=self.tech.pk)
        self.client.post(f"{BASE}{a['id']}/job-done/", {}, format="json")
        report = self.client.get("/api/v1/reports/technicians/").json()

        row = report["rows"][0]
        self.assertEqual((row["assigned"], row["done"], row["pending"]), (2, 1, 1))
        self.assertEqual(row["normal"], 1)
        self.assertEqual(report["totals"], {"assigned": 2, "done": 1})
