from django.core.files.uploadedfile import SimpleUploadedFile

from apps.sales.models import FreeService, Sale, SaleDocument
from apps.sales.test_bike_maintenance import BASE, BikeTestCase


class ServiceKindTests(BikeTestCase):

    def job(self, **extra):
        payload = {
            "sale_type": "maintenance",
            "customer": self.customer.pk,
            "items": [{"description": "Repair", "quantity": "1", "unit_price": "0"}],
            **extra,
        }
        return self.client.post(BASE, payload, format="json")

    def accident(self, advance="5000", **extra):
        extra.setdefault("advance_amount", advance)
        extra.setdefault("advance_method", "cash")

        return self.job(service_kind="accident", **extra)

    def paper(self, sale_id, name="police.pdf"):
        return self.client.post(
            f"{BASE}{sale_id}/documents/",
            {"file": SimpleUploadedFile(name, b"%PDF-1.4 x"), "category": "police_report"},
            format="multipart",
        )

    def bill(self, sale_id, **body):
        return self.client.post(f"{BASE}{sale_id}/confirm/", body, format="json")

    # -- accident --------------------------------------------------------

    def test_an_accident_case_starts_with_an_advance_and_no_final_total(self):
        response = self.accident()

        self.assertEqual(response.status_code, 201, response.data)
        data = response.json()
        self.assertEqual(data["service_kind"], "accident")
        self.assertEqual(data["paid_amount"], "5000.00")

    def test_no_advance_no_accident_case(self):
        response = self.job(service_kind="accident")

        self.assertEqual(response.status_code, 400)
        self.assertIn("advance_amount", response.json())

    def test_the_advance_is_only_for_accident_cases(self):
        response = self.job(advance_amount="100", advance_method="cash")

        self.assertEqual(response.status_code, 400)

    def test_the_final_price_is_needed_to_bill(self):
        sale = self.accident().json()
        response = self.bill(sale["id"], ignore_documents=True)

        self.assertEqual(response.status_code, 400)

    def test_billing_warns_when_papers_are_not_verified(self):
        sale = self.accident().json()
        self.client.patch(
            f"{BASE}{sale['id']}/",
            {"items": [{"description": "Repair", "quantity": "1", "unit_price": "20000"}]},
            format="json",
        )
        warned = self.bill(sale["id"])

        self.assertEqual(warned.status_code, 409)
        self.assertEqual(warned.json()["code"], "documents_unverified")

        self.paper(sale["id"])
        warned = self.bill(sale["id"])
        self.assertEqual(warned.json()["code"], "documents_unverified")

    def test_verified_papers_let_it_through_without_a_flag(self):
        sale = self.accident().json()
        self.client.patch(
            f"{BASE}{sale['id']}/",
            {"items": [{"description": "Repair", "quantity": "1", "unit_price": "20000"}]},
            format="json",
        )
        doc = self.paper(sale["id"]).json()
        ok = self.client.post(
            f"{BASE}{sale['id']}/documents/{doc['id']}/verify/", {}, format="json"
        )
        self.assertTrue(ok.json()["verified"])
        self.assertTrue(ok.json()["verified_by_name"])

        done = self.bill(sale["id"])

        self.assertEqual(done.status_code, 200, done.data)
        self.assertFalse(done.json()["documents_ignored"])

    def test_the_owner_can_go_ahead_and_it_is_noted(self):
        sale = self.accident().json()
        self.client.patch(
            f"{BASE}{sale['id']}/",
            {"items": [{"description": "Repair", "quantity": "1", "unit_price": "20000"}]},
            format="json",
        )
        done = self.bill(sale["id"], ignore_documents=True)

        self.assertEqual(done.status_code, 200, done.data)
        self.assertTrue(done.json()["documents_ignored"])
        self.assertEqual(done.json()["remaining_amount"], "15000.00")

    def test_the_advance_cannot_be_more_than_the_final_total(self):
        sale = self.accident(advance="5000").json()
        response = self.client.patch(
            f"{BASE}{sale['id']}/",
            {"items": [{"description": "Repair", "quantity": "1", "unit_price": "3000"}]},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_an_accident_draft_can_be_edited_before_the_price_is_known(self):
        sale = self.accident(advance="5000").json()
        response = self.client.patch(
            f"{BASE}{sale['id']}/", {"notes": "Front fork bent"}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.data)

    def test_papers_of_a_billed_sale_cannot_be_removed(self):
        sale = self.accident().json()
        self.client.patch(
            f"{BASE}{sale['id']}/",
            {"items": [{"description": "Repair", "quantity": "1", "unit_price": "9000"}]},
            format="json",
        )
        doc = self.paper(sale["id"]).json()
        self.bill(sale["id"], ignore_documents=True)

        gone = self.client.delete(f"{BASE}{sale['id']}/documents/{doc['id']}/")

        self.assertEqual(gone.status_code, 400)
        self.assertTrue(SaleDocument.objects.filter(pk=doc["id"]).exists())

    def test_a_bad_file_type_is_refused(self):
        sale = self.accident().json()
        response = self.client.post(
            f"{BASE}{sale['id']}/documents/",
            {"file": SimpleUploadedFile("run.exe", b"MZ")},
            format="multipart",
        )

        self.assertEqual(response.status_code, 400)

    def test_a_normal_service_still_needs_a_price(self):
        self.assertEqual(self.job().status_code, 400)

    def test_a_stock_sale_makes_no_service_followup(self):
        sale = self.client.post(
            BASE,
            {"sale_type": "stock", "customer": self.customer.pk,
             "items": [{"product": self.model.pk, "quantity": "1", "unit_price": "100"}]},
            format="json",
        ).json()
        self.client.post(
            "/api/v1/sales/payments/",
            {"sale": sale["id"], "amount": "100", "method": "cash"}, format="json",
        )
        self.bill(sale["id"])

        kinds = set(Sale.objects.get(pk=sale["id"]).followups.values_list("kind", flat=True))

        self.assertFalse(kinds & {"maintenance_post", "maintenance_due"})

    # -- free service ----------------------------------------------------

    def bike(self, free=2):
        sale = self.made_bike(free_services=free)
        self.client.post(
            "/api/v1/sales/payments/",
            {"sale": sale["id"], "amount": "100000", "method": "cash"}, format="json",
        )
        self.assertEqual(self.bill(sale["id"]).status_code, 200)

        return sale

    def test_a_free_service_job_is_logged_on_the_bike_sale(self):
        bike = self.bike()
        response = self.job(
            service_kind="free", free_service_for=bike["id"], odometer_km=450
        )

        self.assertEqual(response.status_code, 201, response.data)
        job = response.json()
        self.assertEqual(job["free_service_for_no"], bike["sale_no"])
        self.assertEqual(self.bill(job["id"]).status_code, 200)

        record = FreeService.objects.get()
        self.assertEqual(record.odometer_km, 450)
        self.assertEqual(record.service_sale_id, job["id"])
        detail = self.client.get(f"{BASE}{bike['id']}/").json()
        self.assertEqual(detail["free_services_left"], 1)

    def test_a_free_service_needs_a_bike_sale_and_the_km(self):
        bike = self.bike()

        self.assertEqual(self.job(service_kind="free", odometer_km=1).status_code, 400)
        self.assertEqual(
            self.job(service_kind="free", free_service_for=bike["id"]).status_code, 400
        )

    def test_no_free_service_job_when_none_are_left(self):
        bike = self.bike(free=1)
        first = self.job(
            service_kind="free", free_service_for=bike["id"], odometer_km=450
        ).json()
        self.bill(first["id"])
        again = self.job(
            service_kind="free", free_service_for=bike["id"], odometer_km=3000
        )

        self.assertEqual(again.status_code, 400)

    def test_the_kind_is_for_maintenance_only(self):
        response = self.client.post(
            BASE,
            {"sale_type": "stock", "service_kind": "free",
             "items": [{"product": self.model.pk, "quantity": "1", "unit_price": "100"}]},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
