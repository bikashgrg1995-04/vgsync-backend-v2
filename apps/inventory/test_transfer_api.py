from decimal import Decimal

from django.core.cache import cache
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.businesses.models import Business
from apps.companies.models import Company
from apps.inventory.models import Product, StockBalance

PASSWORD = "TestPass123!"
BASE = "/api/v1/inventory/transfers/"


class TransferAPITests(APITestCase):
    def setUp(self):
        cache.clear()

        self.company = Company.objects.create(name="Order Co", code="ORD-CO")
        self.ho = Business.objects.create(
            name="Order HO", business_type=Business.BusinessType.HEAD_OFFICE,
            company=self.company, business_code="ORD-HO",
        )
        self.branch = Business.objects.create(
            name="Order Branch", business_type=Business.BusinessType.BRANCH,
            company=self.company, parent_business=self.ho, business_code="ORD-BR",
        )
        self.other_branch = Business.objects.create(
            name="Other Branch", business_type=Business.BusinessType.BRANCH,
            company=self.company, parent_business=self.ho, business_code="ORD-B2",
        )

        self.product = Product.objects.create(
            owner_business=self.ho, stock_no="O-1", name="Order Oil",
            purchase_price=Decimal("100"), sale_price=Decimal("150"), mrp=Decimal("160"),
        )
        StockBalance.objects.create(
            product=self.product, business=self.ho, quantity=Decimal("100.000")
        )

        self.ho_owner = User.objects.create_user(
            username="ho-owner", password=PASSWORD, role=User.Role.OWNER, company=self.company
        )
        self.branch_admin = User.objects.create_user(
            username="br-admin", password=PASSWORD, role=User.Role.OWNER,
            business=self.branch,
        )
        self.other_admin = User.objects.create_user(
            username="b2-admin", password=PASSWORD, role=User.Role.OWNER,
            business=self.other_branch,
        )

    def login(self, username, code):
        response = self.client.post(
            "/api/v1/auth/login/",
            {"business_code": code, "username": username, "password": PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")

    def order(self, qty="10"):
        self.login("br-admin", "ORD-BR")
        response = self.client.post(
            BASE, {"items": [{"product": self.product.pk, "quantity": qty}], "notes": "need it"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def test_full_flow_with_damaged_return_and_review(self):
        order = self.order("10")
        self.assertEqual(order["status"], "ordered")
        self.assertTrue(order["reference"].startswith("ORD-ORD-BR-"))
        item_id = order["items"][0]["id"]

        # Branch cannot dispatch; Head Office can.
        response = self.client.post(
            f"{BASE}{order['id']}/dispatch/",
            {"items": [{"transfer_item": item_id, "quantity": "10"}]}, format="json",
        )
        self.assertIn(response.status_code, (400, 403), response.data)

        self.login("ho-owner", "ORD-HO")
        response = self.client.post(
            f"{BASE}{order['id']}/dispatch/",
            {"items": [{"transfer_item": item_id, "quantity": "10"}], "notes": "truck 1"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "dispatched")
        self.assertEqual(response.data["items"][0]["to_receive"], "10.000")

        # Branch receives 8 good + 2 damaged.
        self.login("br-admin", "ORD-BR")
        response = self.client.post(
            f"{BASE}{order['id']}/receive/",
            {"items": [{"transfer_item": item_id, "good_quantity": "8", "damaged_quantity": "2"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "received")
        self.assertEqual(response.data["items"][0]["damaged_to_return"], "2.000")
        balance = StockBalance.objects.get(product=self.product, business=self.branch)
        self.assertEqual(balance.quantity, Decimal("8.000"))
        self.assertEqual(balance.damaged_quantity, Decimal("2.000"))

        # A note is required to return damaged stock.
        response = self.client.post(
            f"{BASE}{order['id']}/return/",
            {"items": [{"transfer_item": item_id, "quantity": "2"}]}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)

        response = self.client.post(
            f"{BASE}{order['id']}/return/",
            {"items": [{"transfer_item": item_id, "quantity": "2"}], "notes": "cracked in transit"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        batch = response.data["returns"][0]
        self.assertEqual(batch["notes"], "cracked in transit")
        self.assertEqual(response.data["items"][0]["damaged_to_return"], "0.000")

        # Head Office receives, then reviews.
        self.login("ho-owner", "ORD-HO")
        response = self.client.post(
            f"{BASE}{order['id']}/returns/{batch['id']}/receive/",
            {"items": [{"return_item": batch["items"][0]["id"], "received_quantity": "2"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        receipt_item = response.data["returns"][0]["receipt"]["items"][0]["id"]

        response = self.client.post(
            f"{BASE}{order['id']}/returns/{batch['id']}/review/",
            {
                "items": [{"receipt_item": receipt_item, "accepted_quantity": "1", "rejected_quantity": "1"}],
                "remarks": "one is fine",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["returns"][0]["review"]["decision"], "partially_accepted")

    def test_orders_are_only_visible_to_the_two_businesses(self):
        order = self.order()

        self.login("b2-admin", "ORD-B2")
        listing = self.client.get(BASE)
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data["count"], 0)
        self.assertEqual(self.client.get(f"{BASE}{order['id']}/").status_code, 404)

        self.login("ho-owner", "ORD-HO")
        self.assertEqual(self.client.get(BASE).data["count"], 1)

    def test_head_office_cannot_place_an_order(self):
        self.login("ho-owner", "ORD-HO")
        response = self.client.post(
            BASE, {"items": [{"product": self.product.pk, "quantity": "1"}]}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.data)

    def test_cancel_only_before_dispatch(self):
        order = self.order()
        item_id = order["items"][0]["id"]

        response = self.client.post(f"{BASE}{order['id']}/cancel/", {}, format="json")
        self.assertEqual(response.status_code, 400)

        response = self.client.post(
            f"{BASE}{order['id']}/cancel/", {"reason": "ordered twice"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "cancelled")
        self.assertIn("ordered twice", response.data["notes"])

        second = self.order()
        self.login("ho-owner", "ORD-HO")
        self.client.post(
            f"{BASE}{second['id']}/dispatch/",
            {"items": [{"transfer_item": second["items"][0]["id"], "quantity": "5"}]}, format="json",
        )
        self.login("br-admin", "ORD-BR")
        response = self.client.post(
            f"{BASE}{second['id']}/cancel/", {"reason": "too late"}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.data)

    def test_cannot_dispatch_more_than_ordered(self):
        order = self.order("5")
        self.login("ho-owner", "ORD-HO")
        response = self.client.post(
            f"{BASE}{order['id']}/dispatch/",
            {"items": [{"transfer_item": order["items"][0]["id"], "quantity": "6"}]}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
