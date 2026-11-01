
from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import Position, PositionPermission, User
from apps.businesses.models import Business
from apps.companies.models import Company
from apps.inventory.models import Product, StockBalance, StockMovement


PASSWORD = "TestPass123!"


class StockAPITests(APITestCase):

    def setUp(self):
        cache.clear()

        self.company = Company.objects.create(
            name="Stock Test Company",
            code="STOCK-CO",
        )
        self.other_company = Company.objects.create(
            name="Other Stock Company",
            code="OTHER-STOCK-CO",
        )

        self.head_office = Business.objects.create(
            name="Stock Test HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=self.company,
            business_code="STOCK-HO",
        )
        self.branch = Business.objects.create(
            name="Stock Test Branch",
            business_type=Business.BusinessType.BRANCH,
            company=self.company,
            parent_business=self.head_office,
            business_code="STOCK-BR",
        )
        self.other_head_office = Business.objects.create(
            name="Other Stock HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=self.other_company,
            business_code="OTHER-STOCK-HO",
        )

        self.product = Product.objects.create(
            owner_business=self.head_office,
            stock_no="STOCK-001",
            name="Stock Product",
            mrp=Decimal("1200.00"),
            purchase_price=Decimal("800.00"),
            sale_price=Decimal("1000.00"),
        )
        self.second_product = Product.objects.create(
            owner_business=self.head_office,
            stock_no="STOCK-002",
            name="Zero Stock Product",
            mrp=Decimal("900.00"),
            purchase_price=Decimal("600.00"),
            sale_price=Decimal("750.00"),
        )
        self.other_product = Product.objects.create(
            owner_business=self.other_head_office,
            stock_no="OTHER-001",
            name="Other Company Product",
            mrp=Decimal("500.00"),
            purchase_price=Decimal("300.00"),
            sale_price=Decimal("400.00"),
        )

        self.stock_url = "/api/v1/inventory/stock/"
        self.movement_url = "/api/v1/inventory/stock/movements/"

    # --------------------------------------------------
    # HELPERS
    # --------------------------------------------------

    def create_user(self, username, **kwargs):
        return User.objects.create_user(
            username=username,
            password=PASSWORD,
            **kwargs,
        )

    def login(self, username, business_code):
        response = self.client.post(
            "/api/v1/auth/login/",
            {
                "business_code": business_code,
                "username": username,
                "password": PASSWORD,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {response.data['access']}"
        )

        return response

    def create_company_owner(self, username="stock-owner"):
        return self.create_user(
            username,
            role=User.Role.OWNER,
            company=self.company,
        )

    def movement_payload(
        self,
        product_id=None,
        movement_type="purchase_receipt",
        quantity="10.000",
        stock_condition="good",
    ):
        return {
            "product_id": product_id or self.product.pk,
            "movement_type": movement_type,
            "quantity": quantity,
            "stock_condition": stock_condition,
        }

    def create_test_movement(
        self,
        *,
        product=None,
        movement_type="purchase_receipt",
        stock_condition="good",
        quantity="5.000",
        reference="",
        notes="",
    ):
        product = product or self.product

        balance, _ = StockBalance.objects.get_or_create(
            product=product,
            business=self.head_office,
        )

        return StockMovement.objects.create(
            stock_balance=balance,
            movement_type=movement_type,
            stock_condition=stock_condition,
            quantity=Decimal(quantity),
            balance_before=Decimal("0.000"),
            balance_after=Decimal(quantity),
            reference=reference,
            notes=notes,
        )

    # --------------------------------------------------
    # STOCK LIST
    # --------------------------------------------------

    def test_head_office_can_view_own_stock(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        StockBalance.objects.create(
            product=self.product,
            business=self.head_office,
            quantity=Decimal("25.500"),
        )

        response = self.client.get(self.stock_url)

        self.assertEqual(response.status_code, 200, response.data)

        stock_by_product = {
            item["product_id"]: item
            for item in response.data
        }

        self.assertEqual(
            stock_by_product[self.product.pk]["quantity"],
            "25.500",
        )
        self.assertEqual(
            stock_by_product[self.second_product.pk]["quantity"],
            "0.000",
        )

    def test_stock_list_includes_products_with_zero_stock(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        response = self.client.get(self.stock_url)

        self.assertEqual(response.status_code, 200, response.data)

        returned_ids = {
            item["product_id"]
            for item in response.data
        }

        self.assertIn(self.product.pk, returned_ids)
        self.assertIn(self.second_product.pk, returned_ids)

    # --------------------------------------------------
    # BRANCH STOCK ISOLATION
    # --------------------------------------------------

    def test_branch_sees_head_office_catalog_but_own_stock_only(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        StockBalance.objects.create(
            product=self.product,
            business=self.head_office,
            quantity=Decimal("100.000"),
        )

        self.login("stock-owner", "STOCK-BR")

        response = self.client.get(self.stock_url)

        self.assertEqual(response.status_code, 200, response.data)

        stock_by_product = {
            item["product_id"]: item
            for item in response.data
        }

        self.assertEqual(
            set(stock_by_product),
            {self.product.pk, self.second_product.pk},
        )

        self.assertEqual(
            stock_by_product[self.product.pk]["quantity"],
            "0.000",
        )

    def test_branch_sees_its_own_stock_balance(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-BR")

        StockBalance.objects.create(
            product=self.product,
            business=self.branch,
            quantity=Decimal("15.250"),
        )

        response = self.client.get(self.stock_url)

        self.assertEqual(response.status_code, 200, response.data)

        stock = next(
            item
            for item in response.data
            if item["product_id"] == self.product.pk
        )

        self.assertEqual(stock["quantity"], "15.250")

    # --------------------------------------------------
    # STOCK DETAIL
    # --------------------------------------------------

    def test_stock_detail_returns_zero_balance_when_no_balance_exists(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        response = self.client.get(
            f"{self.stock_url}{self.second_product.pk}/"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["quantity"], "0.000")
        self.assertEqual(response.data["damaged_quantity"], "0.000")

    def test_stock_detail_does_not_expose_another_company_product(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        response = self.client.get(
            f"{self.stock_url}{self.other_product.pk}/"
        )

        self.assertEqual(response.status_code, 404)

    # --------------------------------------------------
    # STOCK MOVEMENT CREATION
    # --------------------------------------------------

    def test_head_office_can_create_purchase_receipt(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        response = self.client.post(
            self.movement_url,
            self.movement_payload(quantity="20.000"),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)

        balance = StockBalance.objects.get(
            product=self.product,
            business=self.head_office,
        )

        self.assertEqual(balance.quantity, Decimal("20.000"))
        self.assertEqual(balance.damaged_quantity, Decimal("0.000"))

        self.assertEqual(
            StockMovement.objects.filter(
                stock_balance=balance,
            ).count(),
            1,
        )

    def test_damaged_stock_is_tracked_separately(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        response = self.client.post(
            self.movement_url,
            self.movement_payload(
                quantity="5.000",
                stock_condition="damaged",
            ),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)

        balance = StockBalance.objects.get(
            product=self.product,
            business=self.head_office,
        )

        self.assertEqual(balance.quantity, Decimal("0.000"))
        self.assertEqual(
            balance.damaged_quantity,
            Decimal("5.000"),
        )

    def test_insufficient_stock_returns_400_without_changing_balance(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        balance = StockBalance.objects.create(
            product=self.product,
            business=self.head_office,
            quantity=Decimal("2.000"),
        )

        response = self.client.post(
            self.movement_url,
            self.movement_payload(
                movement_type="sale_issue",
                quantity="5.000",
            ),
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)

        balance.refresh_from_db()

        self.assertEqual(balance.quantity, Decimal("2.000"))

        self.assertFalse(
            StockMovement.objects.filter(
                stock_balance=balance,
            ).exists()
        )

    # --------------------------------------------------
    # BRANCH WRITE RESTRICTIONS
    # --------------------------------------------------

    def test_company_owner_cannot_create_movement_from_branch_context(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-BR")

        response = self.client.post(
            self.movement_url,
            self.movement_payload(),
            format="json",
        )

        self.assertEqual(response.status_code, 403, response.data)

        self.assertFalse(
            StockBalance.objects.filter(
                product=self.product,
                business=self.branch,
            ).exists()
        )

    # --------------------------------------------------
    # MOVEMENT HISTORY ISOLATION
    # --------------------------------------------------

    def test_branch_cannot_view_head_office_movement_history(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        create_response = self.client.post(
            self.movement_url,
            self.movement_payload(),
            format="json",
        )

        self.assertEqual(
            create_response.status_code,
            201,
            create_response.data,
        )

        movement_id = create_response.data["id"]

        self.login("stock-owner", "STOCK-BR")

        list_response = self.client.get(self.movement_url)

        self.assertEqual(
            list_response.status_code,
            200,
            list_response.data,
        )
        self.assertEqual(list_response.data["count"], 0)
        self.assertEqual(list_response.data["results"], [])
        self.assertIsNone(list_response.data["next"])
        self.assertIsNone(list_response.data["previous"])

        detail_response = self.client.get(
            f"{self.movement_url}{movement_id}/"
        )

        self.assertEqual(detail_response.status_code, 404)

    # --------------------------------------------------
    # MOVEMENT HISTORY PAGINATION
    # --------------------------------------------------

    def test_movement_history_uses_default_page_size(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        for index in range(30):
            self.create_test_movement(
                reference=f"PAGE-{index:03d}",
            )

        response = self.client.get(self.movement_url)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 30)
        self.assertEqual(len(response.data["results"]), 25)
        self.assertIsNotNone(response.data["next"])
        self.assertIsNone(response.data["previous"])

    def test_movement_history_respects_page_size_parameter(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        for index in range(30):
            self.create_test_movement(
                reference=f"SIZE-{index:03d}",
            )

        response = self.client.get(
            self.movement_url,
            {"page_size": 10},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 30)
        self.assertEqual(len(response.data["results"]), 10)

    def test_movement_history_page_size_is_limited_to_100(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        for index in range(105):
            self.create_test_movement(
                reference=f"LIMIT-{index:03d}",
            )

        response = self.client.get(
            self.movement_url,
            {"page_size": 999},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 105)
        self.assertEqual(len(response.data["results"]), 100)

    # --------------------------------------------------
    # MOVEMENT HISTORY FILTERS
    # --------------------------------------------------

    def test_movement_history_filters_by_product(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        self.create_test_movement(
            product=self.product,
            reference="PRODUCT-FIRST",
        )
        second = self.create_test_movement(
            product=self.second_product,
            reference="PRODUCT-SECOND",
        )

        response = self.client.get(
            self.movement_url,
            {"product_id": self.second_product.pk},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(
            response.data["results"][0]["id"],
            second.pk,
        )

    def test_movement_history_filters_by_movement_type(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        self.create_test_movement(
            movement_type="purchase_receipt",
            reference="PURCHASE-FILTER",
        )
        sale_return = self.create_test_movement(
            movement_type="sale_return",
            reference="RETURN-FILTER",
        )

        response = self.client.get(
            self.movement_url,
            {"movement_type": "sale_return"},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(
            response.data["results"][0]["id"],
            sale_return.pk,
        )

    def test_movement_history_filters_by_stock_condition(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        self.create_test_movement(
            stock_condition="good",
            reference="GOOD-FILTER",
        )
        damaged = self.create_test_movement(
            stock_condition="damaged",
            reference="DAMAGED-FILTER",
        )

        response = self.client.get(
            self.movement_url,
            {"stock_condition": "damaged"},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(
            response.data["results"][0]["id"],
            damaged.pk,
        )

    def test_movement_history_filters_by_date_range(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        movement = self.create_test_movement(
            reference="DATE-FILTER",
        )

        today = timezone.localdate().isoformat()

        response = self.client.get(
            self.movement_url,
            {
                "date_from": today,
                "date_to": today,
            },
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(
            response.data["results"][0]["id"],
            movement.pk,
        )

    def test_movement_history_rejects_invalid_date_range(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        response = self.client.get(
            self.movement_url,
            {
                "date_from": "2026-10-31",
                "date_to": "2026-10-01",
            },
        )

        self.assertEqual(response.status_code, 400, response.data)

    # --------------------------------------------------
    # MOVEMENT HISTORY SEARCH AND ORDERING
    # --------------------------------------------------

    def test_movement_history_searches_reference_and_notes(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        matched = self.create_test_movement(
            reference="INVOICE-SEARCH-123",
            notes="Special inventory delivery",
        )
        self.create_test_movement(
            reference="OTHER-REFERENCE",
            notes="Regular delivery",
        )

        response = self.client.get(
            self.movement_url,
            {"search": "INVOICE-SEARCH-123"},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(
            response.data["results"][0]["id"],
            matched.pk,
        )

    def test_movement_history_can_be_ordered_by_quantity(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        self.create_test_movement(quantity="2.000")
        self.create_test_movement(quantity="8.000")

        response = self.client.get(
            self.movement_url,
            {"ordering": "quantity"},
        )

        self.assertEqual(response.status_code, 200, response.data)

        quantities = [
            Decimal(item["quantity"])
            for item in response.data["results"]
        ]

        self.assertEqual(quantities, sorted(quantities))

    # --------------------------------------------------
    # STAFF MODULE PERMISSIONS
    # --------------------------------------------------

    def test_staff_can_view_stock_with_view_permission(self):
        position = Position.objects.create(
            business=self.head_office,
            name="Stock View Staff",
        )

        self.create_user(
            "stock-staff-view",
            role=User.Role.STAFF,
            company=self.company,
            position=position,
        )

        PositionPermission.objects.create(
            position=position,
            module=PositionPermission.Module.INVENTORY,
            can_view=True,
        )

        self.login("stock-staff-view", "STOCK-HO")

        response = self.client.get(self.stock_url)

        self.assertEqual(response.status_code, 200, response.data)

    def test_staff_without_view_permission_cannot_view_stock(self):
        position = Position.objects.create(
            business=self.head_office,
            name="Stock No View Staff",
        )

        self.create_user(
            "stock-staff-no-view",
            role=User.Role.STAFF,
            company=self.company,
            position=position,
        )

        PositionPermission.objects.create(
            position=position,
            module=PositionPermission.Module.INVENTORY,
            can_create=True,
        )

        self.login("stock-staff-no-view", "STOCK-HO")

        response = self.client.get(self.stock_url)

        self.assertEqual(response.status_code, 403, response.data)

    def test_staff_can_create_movement_with_create_permission(self):
        position = Position.objects.create(
            business=self.head_office,
            name="Stock Create Staff",
        )

        self.create_user(
            "stock-staff-create",
            role=User.Role.STAFF,
            company=self.company,
            position=position,
        )

        PositionPermission.objects.create(
            position=position,
            module=PositionPermission.Module.INVENTORY,
            can_create=True,
        )

        self.login("stock-staff-create", "STOCK-HO")

        response = self.client.post(
            self.movement_url,
            self.movement_payload(),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)

    def test_staff_without_create_permission_cannot_create_movement(self):
        position = Position.objects.create(
            business=self.head_office,
            name="Stock No Create Staff",
        )

        self.create_user(
            "stock-staff-no-create",
            role=User.Role.STAFF,
            company=self.company,
            position=position,
        )

        PositionPermission.objects.create(
            position=position,
            module=PositionPermission.Module.INVENTORY,
            can_view=True,
        )

        self.login("stock-staff-no-create", "STOCK-HO")

        response = self.client.post(
            self.movement_url,
            self.movement_payload(),
            format="json",
        )

        self.assertEqual(response.status_code, 403, response.data)

    # --------------------------------------------------
    # MOVEMENT VALIDATION EDGE CASES
    # --------------------------------------------------

    def test_generic_api_rejects_transfer_and_reversal_types(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        invalid_types = [
            "transfer_in",
            "transfer_out",
            "reversal",
            "invalid_type",
        ]

        for movement_type in invalid_types:
            with self.subTest(movement_type=movement_type):
                response = self.client.post(
                    self.movement_url,
                    self.movement_payload(
                        movement_type=movement_type,
                    ),
                    format="json",
                )

                self.assertEqual(
                    response.status_code,
                    400,
                    response.data,
                )

        self.assertFalse(
            StockBalance.objects.filter(
                product=self.product,
                business=self.head_office,
            ).exists()
        )

    def test_invalid_stock_condition_is_rejected(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        response = self.client.post(
            self.movement_url,
            self.movement_payload(
                stock_condition="unknown",
            ),
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)

        self.assertFalse(
            StockBalance.objects.filter(
                product=self.product,
                business=self.head_office,
            ).exists()
        )

    def test_invalid_quantities_are_rejected(self):
        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        invalid_quantities = [
            "0",
            "0.000",
            "-1.000",
            "1.2345",
            "NaN",
            "Infinity",
            "100000000000.000",
            10,
            10.5,
        ]

        for quantity in invalid_quantities:
            with self.subTest(quantity=quantity):
                response = self.client.post(
                    self.movement_url,
                    self.movement_payload(quantity=quantity),
                    format="json",
                )

                self.assertEqual(
                    response.status_code,
                    400,
                    response.data,
                )

        self.assertFalse(
            StockBalance.objects.filter(
                product=self.product,
                business=self.head_office,
            ).exists()
        )

    def test_inactive_product_cannot_receive_stock_movement(self):
        self.product.is_active = False
        self.product.save(update_fields=["is_active"])

        self.create_company_owner()
        self.login("stock-owner", "STOCK-HO")

        response = self.client.post(
            self.movement_url,
            self.movement_payload(),
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)

        self.assertFalse(
            StockBalance.objects.filter(
                product=self.product,
                business=self.head_office,
            ).exists()
        )

    # --------------------------------------------------
    # AUTHENTICATION
    # --------------------------------------------------

    def test_unauthenticated_user_cannot_view_stock(self):
        response = self.client.get(self.stock_url)

        self.assertEqual(response.status_code, 401)

    def test_unauthenticated_user_cannot_create_movement(self):
        response = self.client.post(
            self.movement_url,
            self.movement_payload(),
            format="json",
        )

        self.assertEqual(response.status_code, 401)
