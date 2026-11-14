from datetime import timedelta

from django.utils import timezone

from apps.sales.models import FreeService
from apps.sales.test_bike_maintenance import BASE, BikeTestCase


class FreeServiceTests(BikeTestCase):

    def confirmed(self, free=3, **extra):
        sale = self.made_bike(free_services=free, **extra)
        response = self.client.post(f"{BASE}{sale['id']}/confirm/")
        self.assertEqual(response.status_code, 200, response.data)

        return response.json()

    def url(self, sale, tail="free-services/"):
        return f"{BASE}{sale['id']}/{tail}"

    def add(self, sale, km=500, **extra):
        return self.client.post(
            self.url(sale), {"odometer_km": km, **extra}, format="json"
        )

    def test_the_sale_says_how_many_are_included_and_left(self):
        sale = self.confirmed(free=3)

        self.assertEqual(sale["free_services"], 3)
        self.assertEqual(sale["free_services_used"], 0)
        self.assertEqual(sale["free_services_left"], 3)

    def test_a_service_is_recorded_with_km_and_performance(self):
        sale = self.confirmed(free=3)
        response = self.add(
            sale, km=520, performance="Smooth", work_done="Oil change",
            technician="Ram",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.json()["number"], 1)
        self.assertEqual(response.json()["odometer_km"], 520)

        detail = self.client.get(f"{BASE}{sale['id']}/").json()

        self.assertEqual(detail["free_services_used"], 1)
        self.assertEqual(detail["free_services_left"], 2)
        self.assertEqual(detail["free_service_records"][0]["performance"], "Smooth")

    def test_the_numbers_follow_each_other(self):
        sale = self.confirmed(free=2)
        self.add(sale, km=500)
        response = self.add(sale, km=3000)

        self.assertEqual(response.json()["number"], 2)

    def test_no_more_than_included(self):
        sale = self.confirmed(free=1)
        self.add(sale, km=500)

        self.assertEqual(self.add(sale, km=3000).status_code, 400)

    def test_a_sale_with_none_included_takes_none(self):
        sale = self.confirmed(free=0)

        self.assertEqual(self.add(sale).status_code, 400)

    def test_km_cannot_go_backwards(self):
        sale = self.confirmed(free=3)
        self.add(sale, km=3000)
        response = self.add(sale, km=2000)

        self.assertEqual(response.status_code, 400)
        self.assertIn("odometer_km", response.json())

    def test_the_date_cannot_be_future_or_before_the_sale(self):
        sale = self.confirmed(free=3)
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        before = (timezone.localdate() - timedelta(days=5)).isoformat()

        self.assertEqual(self.add(sale, service_date=tomorrow).status_code, 400)
        self.assertEqual(self.add(sale, service_date=before).status_code, 400)

    def test_a_draft_cannot_take_records(self):
        sale = self.made_bike(free_services=2)

        self.assertEqual(self.add(sale).status_code, 400)

    def test_only_bike_sales_have_free_services(self):
        response = self.client.post(
            BASE,
            {"sale_type": "stock", "free_services": 2, "items": []},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_only_the_latest_can_be_removed(self):
        sale = self.confirmed(free=3)
        first = self.add(sale, km=500).json()
        second = self.add(sale, km=3000).json()

        gone = self.client.delete(self.url(sale, f"free-services/{first['id']}/"))
        self.assertEqual(gone.status_code, 400)

        gone = self.client.delete(self.url(sale, f"free-services/{second['id']}/"))
        self.assertEqual(gone.status_code, 204)
        self.assertEqual(FreeService.objects.count(), 1)

    def test_the_included_count_can_change_but_not_below_used(self):
        sale = self.confirmed(free=3)
        self.add(sale, km=500)
        self.add(sale, km=3000)

        low = self.client.post(
            self.url(sale, "free-service-count/"), {"free_services": 1}, format="json"
        )
        self.assertEqual(low.status_code, 400)

        ok = self.client.post(
            self.url(sale, "free-service-count/"), {"free_services": 4}, format="json"
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["free_services_left"], 2)

    def test_a_sale_cannot_be_deleted(self):
        sale = self.confirmed()

        self.assertEqual(self.client.delete(f"{BASE}{sale['id']}/").status_code, 405)
