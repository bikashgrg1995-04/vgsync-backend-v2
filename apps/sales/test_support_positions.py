from datetime import date
from decimal import Decimal

from apps.accounts import rbac
from apps.common.testing import User
from apps.inventory.models import Product
from apps.sales.models import Followup, Sale
from apps.sales.test_bike_maintenance import BASE, BikeTestCase

FOLLOWUPS = "/api/v1/sales/followups/"
PAYMENTS = "/api/v1/sales/payments/"
PRODUCTS = "/api/v1/inventory/products/"


class SupportPositionTests(BikeTestCase):
    """Store keeper, telecaller and EMI officer."""

    def setUp(self):
        super().setUp()
        self.users = {}

        for key, label in (
            ("telecaller", "Telecaller"), ("emi_officer", "EMI"),
            ("store_keeper", "Store"), ("sales_staff", "Seller"),
        ):
            pos = self.make_position(self.ho, label)
            rbac.write_position_codes(pos, rbac.preset_codes(key))
            self.users[key] = self.make_user(
                key, User.Role.STAFF, company=self.company, position=pos
            )

        # A confirmed sale with credit left makes a follow-up.
        self.authenticate("ACME-HO", "owner")
        self.sale = self.client.post(
            BASE,
            {
                "sale_type": "maintenance", "customer": self.customer.pk,
                "items": [{"description": "Service", "quantity": "1", "unit_price": "500"}],
            },
            format="json",
        ).json()
        self.followup = Followup.objects.create(
            business=self.ho, sale_id=self.sale["id"], customer=self.customer,
            kind=Followup.Kind.MAINTENANCE_POST, due_date=date.today(),
        )

    def done(self, who):
        self.authenticate("ACME-HO", who)

        return self.client.post(
            f"{FOLLOWUPS}{self.followup.pk}/done/",
            {"note": "Called, bike fine", "has_issue": False},
            format="json",
        )

    def test_a_telecaller_sees_all_followups_and_closes_them(self):
        self.authenticate("ACME-HO", "telecaller")
        listing = self.client.get(FOLLOWUPS).json()["results"]

        self.assertEqual([f["id"] for f in listing], [self.followup.pk])
        self.assertEqual(self.done("telecaller").status_code, 200)

    def test_a_telecaller_cannot_sell_edit_or_take_money(self):
        self.authenticate("ACME-HO", "telecaller")

        self.assertEqual(
            self.client.post(
                BASE,
                {"sale_type": "maintenance", "customer": self.customer.pk,
                 "items": [{"description": "x", "quantity": "1", "unit_price": "1"}]},
                format="json",
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.patch(f"{BASE}{self.sale['id']}/", {"notes": "x"}, format="json").status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                PAYMENTS, {"sale": self.sale["id"], "amount": "10", "method": "cash"},
                format="json",
            ).status_code,
            403,
        )

    def test_someone_without_the_followup_right_cannot_close_one(self):
        # Sales staff keep it only if they can create sales (they can);
        # strip it to prove the check.
        pos = self.users["sales_staff"].position
        rbac.write_position_codes(
            pos, rbac.preset_codes("sales_staff") - {"sales.followup"}
        )

        self.assertEqual(self.done("sales_staff").status_code, 403)

    def test_an_emi_officer_collects_but_does_not_edit(self):
        self.authenticate("ACME-HO", "emi_officer")
        paid = self.client.post(
            PAYMENTS, {"sale": self.sale["id"], "amount": "100", "method": "cash"},
            format="json",
        )

        self.assertEqual(paid.status_code, 201, paid.content)
        self.assertEqual(self.done("emi_officer").status_code, 200)
        self.authenticate("ACME-HO", "emi_officer")
        self.assertEqual(
            self.client.patch(f"{BASE}{self.sale['id']}/", {"notes": "x"}, format="json").status_code,
            403,
        )

    def test_a_store_keeper_handles_stock_but_not_sales_or_cost(self):
        part = Product.objects.create(
            owner_business=self.ho, stock_no="STORE-TEST-1", name="Oil",
            purchase_price=Decimal("90.00"), sale_price=Decimal("150.00"),
        )
        self.authenticate("ACME-HO", "store_keeper")
        listing = self.client.get(PRODUCTS).json()
        listing = listing["results"] if isinstance(listing, dict) else listing
        row = next(p for p in listing if p["id"] == part.pk)

        self.assertNotIn("purchase_price", row)
        self.assertEqual(self.client.get(BASE).status_code, 403)
        self.assertEqual(self.client.get(FOLLOWUPS).status_code, 403)
        self.assertEqual(
            self.client.get("/api/v1/finance/expenses/").status_code, 403
        )
