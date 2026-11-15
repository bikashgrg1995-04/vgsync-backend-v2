from decimal import Decimal

from apps.accounts import rbac
from apps.common.testing import User
from apps.inventory.models import Product, StockMovement
from apps.inventory.services.stock import create_stock_movement
from apps.notifications.models import Notification
from apps.sales.models import JobLine
from apps.sales.test_bike_maintenance import BASE, BikeTestCase

MY = "/api/v1/sales/my-jobs/"
PRODUCTS = "/api/v1/inventory/products/"


class JobLineTests(BikeTestCase):

    def setUp(self):
        super().setUp()
        pos = self.make_position(self.ho, "Technician")
        rbac.write_position_codes(pos, rbac.preset_codes("technician"))
        self.tech = self.make_user("tech", User.Role.STAFF, company=self.company, position=pos)
        self.part = Product.objects.create(
            owner_business=self.ho, stock_no="PAD-1", name="Brake pad",
            purchase_price=Decimal("150.00"), sale_price=Decimal("400.00"),
        )
        create_stock_movement(
            user=self.owner, business=self.ho, product=self.part,
            movement_type=StockMovement.MovementType.OPENING, quantity="20",
        )
        self.authenticate("ACME-HO", "owner")
        self.job = self.client.post(
            BASE,
            {
                "sale_type": "maintenance", "customer": self.customer.pk,
                "technician": self.tech.pk,
                "items": [{"description": "Service", "quantity": "1", "unit_price": "500"}],
            },
            format="json",
        ).json()

    def ask(self, **body):
        self.authenticate("ACME-HO", "tech")

        return self.client.post(f"{MY}{self.job['id']}/lines/", body, format="json")

    def line(self):
        return JobLine.objects.get(sale_id=self.job["id"])

    def office(self, path, body=None):
        self.authenticate("ACME-HO", "owner")

        return self.client.post(f"{BASE}{self.job['id']}/job-lines/{self.line().pk}/{path}/", body or {}, format="json")

    def test_the_technician_asks_for_a_part_and_sees_it_without_a_price(self):
        made = self.ask(kind="part", product=self.part.pk, quantity="2")

        self.assertEqual(made.status_code, 201, made.content)
        row = made.json()["lines"][0]
        self.assertEqual(row["name"], "Brake pad")
        self.assertEqual(row["status"], "requested")
        self.assertNotIn("unit_price", row)

    def test_the_part_price_defaults_to_the_sale_price_when_approved(self):
        self.ask(kind="part", product=self.part.pk, quantity="2")
        done = self.office("approve")

        self.assertEqual(done.status_code, 200, done.content)
        self.assertEqual(done.json()["unit_price"], "400.00")
        sale = self.client.get(f"{BASE}{self.job['id']}/").json()
        self.assertEqual(len(sale["items"]), 2)
        self.assertEqual(sale["net_total"], "1300.00")  # 500 + 2 x 400

    def test_work_needs_a_price_from_the_office(self):
        self.ask(kind="work", description="Rewired the horn")

        self.assertEqual(self.office("approve").status_code, 400)
        self.assertEqual(self.office("approve", {"unit_price": "250"}).status_code, 200)
        sale = self.client.get(f"{BASE}{self.job['id']}/").json()
        self.assertEqual(sale["net_total"], "750.00")

    def test_turning_it_down_tells_the_technician(self):
        self.ask(kind="part", product=self.part.pk)
        done = self.office("reject", {"reason": "Customer has his own"})

        self.assertEqual(done.status_code, 200)
        self.assertEqual(self.line().status, "rejected")
        self.assertTrue(
            Notification.objects.filter(recipient=self.tech, kind="job_line_decided").exists()
        )
        sale = self.client.get(f"{BASE}{self.job['id']}/").json()
        self.assertEqual(sale["net_total"], "500.00")

    def test_a_decided_request_cannot_be_decided_again(self):
        self.ask(kind="part", product=self.part.pk)
        self.office("approve")

        self.assertEqual(self.office("approve").status_code, 409)

    def test_the_office_is_told_of_a_request(self):
        self.ask(kind="part", product=self.part.pk)

        self.assertTrue(
            Notification.objects.filter(kind="job_line_requested").exists()
        )

    def test_it_can_be_withdrawn_until_decided(self):
        self.ask(kind="part", product=self.part.pk)
        self.authenticate("ACME-HO", "tech")
        gone = self.client.delete(f"{MY}{self.job['id']}/lines/{self.line().pk}/")

        self.assertEqual(gone.status_code, 200, gone.content)
        self.assertEqual(self.line().status, "withdrawn")

    def test_a_billed_job_takes_no_more_requests(self):
        self.authenticate("ACME-HO", "owner")
        self.client.post(f"{BASE}{self.job['id']}/confirm/", {}, format="json")

        self.assertEqual(self.ask(kind="part", product=self.part.pk).status_code, 409)

    def test_someone_elses_job_is_not_theirs(self):
        other_pos = self.make_position(self.ho, "Tech2")
        rbac.write_position_codes(other_pos, rbac.preset_codes("technician"))
        self.make_user("tech2", User.Role.STAFF, company=self.company, position=other_pos)
        self.authenticate("ACME-HO", "tech2")

        response = self.client.post(
            f"{MY}{self.job['id']}/lines/", {"kind": "work", "description": "x"}, format="json"
        )

        self.assertEqual(response.status_code, 404)

    def test_the_kind_must_match(self):
        self.assertEqual(self.ask(kind="part", description="loose").status_code, 400)
        self.assertEqual(self.ask(kind="work").status_code, 400)

    def test_a_technician_cannot_decide(self):
        self.ask(kind="part", product=self.part.pk)
        self.authenticate("ACME-HO", "tech")
        url = f"{BASE}{self.job['id']}/job-lines/{self.line().pk}/approve/"

        self.assertEqual(self.client.post(url, {}, format="json").status_code, 403)


class CostHiddenTests(BikeTestCase):

    def test_a_technician_sees_stock_prices_but_not_purchase_cost(self):
        pos = self.make_position(self.ho, "Technician")
        rbac.write_position_codes(pos, rbac.preset_codes("technician"))
        self.make_user("tech", User.Role.STAFF, company=self.company, position=pos)
        part = Product.objects.create(
            owner_business=self.ho, stock_no="PAD-1", name="Brake pad",
            purchase_price=Decimal("150.00"), sale_price=Decimal("400.00"),
        )
        self.authenticate("ACME-HO", "owner")
        owner_row = self.client.get(f"{PRODUCTS}{part.pk}/").json()
        self.assertEqual(owner_row["purchase_price"], "150.00")

        self.authenticate("ACME-HO", "tech")
        row = self.client.get(f"{PRODUCTS}{part.pk}/").json()
        self.assertEqual(row["sale_price"], "400.00")
        self.assertNotIn("purchase_price", row)

        listing = self.client.get(PRODUCTS).json()
        results = listing["results"] if isinstance(listing, dict) else listing
        self.assertTrue(results)
        self.assertTrue(all("purchase_price" not in r for r in results))

    def test_the_catalog_carries_the_new_code(self):
        self.assertIn("inventory.view_cost", rbac.preset_codes("administrator"))
        self.assertNotIn("inventory.view_cost", rbac.preset_codes("technician"))
