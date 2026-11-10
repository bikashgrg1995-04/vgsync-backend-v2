
from decimal import Decimal

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.accounts.models import Position, PositionPermission, User
from apps.businesses.models import Business
from apps.companies.models import Company
from apps.inventory.models import Product, StockBalance


PASSWORD = "TestPass123!"


class ProductAPITests(APITestCase):

    def setUp(self):
        cache.clear()

        self.company = Company.objects.create(
            name="Product Test Company",
            code="PRODUCT-CO",
        )
        self.other_company = Company.objects.create(
            name="Other Product Company",
            code="OTHER-PRODUCT-CO",
        )

        self.head_office = Business.objects.create(
            name="Product Test HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=self.company,
            business_code="PRODUCT-HO",
        )
        self.branch = Business.objects.create(
            name="Product Test Branch",
            business_type=Business.BusinessType.BRANCH,
            company=self.company,
            parent_business=self.head_office,
            business_code="PRODUCT-BR",
        )
        self.other_head_office = Business.objects.create(
            name="Other Product HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=self.other_company,
            business_code="OTHER-PRODUCT-HO",
        )
        self.independent = Business.objects.create(
            name="Product Independent",
            business_type=Business.BusinessType.INDEPENDENT,
            business_code="PRODUCT-IND",
        )

        self.head_office_product = Product.objects.create(
            owner_business=self.head_office,
            stock_no="HO-001",
            name="Head Office Product",
            mrp=Decimal("1200.00"),
            purchase_price=Decimal("800.00"),
            sale_price=Decimal("1000.00"),
        )
        self.second_head_office_product = Product.objects.create(
            owner_business=self.head_office,
            stock_no="HO-002",
            name="Second Head Office Product",
            mrp=Decimal("900.00"),
            purchase_price=Decimal("600.00"),
            sale_price=Decimal("750.00"),
        )
        self.other_company_product = Product.objects.create(
            owner_business=self.other_head_office,
            stock_no="OTHER-001",
            name="Other Company Product",
            mrp=Decimal("500.00"),
            purchase_price=Decimal("300.00"),
            sale_price=Decimal("400.00"),
        )
        self.independent_product = Product.objects.create(
            owner_business=self.independent,
            stock_no="IND-001",
            name="Independent Product",
            mrp=Decimal("700.00"),
            purchase_price=Decimal("400.00"),
            sale_price=Decimal("550.00"),
        )

        self.list_url = "/api/v1/inventory/products/"

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

    def product_payload(self, stock_no="NEW-001", name="New Product"):
        return {
            "stock_no": stock_no,
            "name": name,
            "mrp": "1200.00",
            "purchase_price": "800.00",
            "sale_price": "1000.00",
        }

    # --------------------------------------------------
    # HEAD OFFICE CATALOG
    # --------------------------------------------------

    def test_head_office_owner_can_list_own_catalog(self):
        self.create_user(
            "product-ho-owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        self.login("product-ho-owner", "PRODUCT-HO")

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200, response.data)

        returned_ids = {item["id"] for item in response.data}

        self.assertEqual(
            returned_ids,
            {
                self.head_office_product.id,
                self.second_head_office_product.id,
            },
        )

    def test_head_office_owner_can_create_product(self):
        self.create_user(
            "product-create-owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        self.login("product-create-owner", "PRODUCT-HO")

        response = self.client.post(
            self.list_url,
            self.product_payload(),
            format="json",
        )

        self.assertEqual(
            response.status_code,
            201,
            response.data,
        )
        self.assertTrue(
            Product.objects.filter(
                owner_business=self.head_office,
                stock_no="NEW-001",
            ).exists()
        )

    def test_head_office_owner_can_update_product(self):
        self.create_user(
            "product-update-owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        self.login("product-update-owner", "PRODUCT-HO")

        response = self.client.patch(
            f"{self.list_url}{self.head_office_product.pk}/",
            {"name": "Updated Product"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)

        self.head_office_product.refresh_from_db()
        self.assertEqual(
            self.head_office_product.name,
            "Updated Product",
        )

    def test_owner_can_upload_and_remove_product_image(self):
        import shutil
        import tempfile

        from django.core.files.uploadedfile import SimpleUploadedFile

        media_root = tempfile.mkdtemp()

        try:
            with override_settings(MEDIA_ROOT=media_root):
                self.create_user(
                    "product-image-owner",
                    role=User.Role.OWNER,
                    company=self.company,
                )
                self.login("product-image-owner", "PRODUCT-HO")

                url = f"{self.list_url}{self.head_office_product.pk}/"

                # 1x1 transparent GIF
                gif = (
                    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00"
                    b"\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00"
                    b"\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
                )

                response = self.client.patch(
                    url,
                    {
                        "name": "With Image",
                        "image": SimpleUploadedFile(
                            "part.gif",
                            gif,
                            content_type="image/gif",
                        ),
                    },
                    format="multipart",
                )

                self.assertEqual(response.status_code, 200, response.data)
                self.assertTrue(response.data["image"])

                self.head_office_product.refresh_from_db()
                self.assertTrue(self.head_office_product.image)

                # An empty image value clears the picture.
                response = self.client.patch(
                    url,
                    {"image": ""},
                    format="multipart",
                )

                self.assertEqual(response.status_code, 200, response.data)

                self.head_office_product.refresh_from_db()
                self.assertFalse(self.head_office_product.image)
        finally:
            shutil.rmtree(media_root, ignore_errors=True)

    # --------------------------------------------------
    # BRANCH SHARED CATALOG
    # --------------------------------------------------

    def test_branch_can_view_parent_head_office_catalog(self):
        self.create_user(
            "product-branch-owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        self.login("product-branch-owner", "PRODUCT-BR")

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200, response.data)

        returned_ids = {item["id"] for item in response.data}

        self.assertEqual(
            returned_ids,
            {
                self.head_office_product.id,
                self.second_head_office_product.id,
            },
        )

    def test_company_owner_cannot_create_product_from_branch_context(self):
        self.create_user(
            "product-branch-create-owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        self.login("product-branch-create-owner", "PRODUCT-BR")

        response = self.client.post(
            self.list_url,
            self.product_payload(),
            format="json",
        )

        self.assertEqual(response.status_code, 403, response.data)

    def test_branch_admin_cannot_create_central_product(self):
        self.create_user(
            "product-branch-admin",
            role=User.Role.OWNER,
            business=self.branch,
        )
        self.login("product-branch-admin", "PRODUCT-BR")

        response = self.client.post(
            self.list_url,
            self.product_payload(),
            format="json",
        )

        self.assertEqual(response.status_code, 403, response.data)

    def test_branch_cannot_update_parent_product(self):
        self.create_user(
            "product-branch-update-admin",
            role=User.Role.OWNER,
            business=self.branch,
        )
        self.login("product-branch-update-admin", "PRODUCT-BR")

        response = self.client.patch(
            f"{self.list_url}{self.head_office_product.pk}/",
            {"name": "Branch Modified Product"},
            format="json",
        )

        self.assertEqual(response.status_code, 403, response.data)

        self.head_office_product.refresh_from_db()
        self.assertEqual(
            self.head_office_product.name,
            "Head Office Product",
        )

    # --------------------------------------------------
    # INDEPENDENT BUSINESS CATALOG
    # --------------------------------------------------

    def test_independent_business_can_only_view_own_catalog(self):
        self.create_user(
            "product-independent-owner",
            role=User.Role.OWNER,
            business=self.independent,
        )
        self.login("product-independent-owner", "PRODUCT-IND")

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200, response.data)

        returned_ids = {item["id"] for item in response.data}

        self.assertEqual(
            returned_ids,
            {self.independent_product.id},
        )

    def test_independent_business_can_create_own_product(self):
        self.create_user(
            "product-independent-create",
            role=User.Role.OWNER,
            business=self.independent,
        )
        self.login("product-independent-create", "PRODUCT-IND")

        response = self.client.post(
            self.list_url,
            self.product_payload("IND-NEW-001"),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)

        self.assertTrue(
            Product.objects.filter(
                owner_business=self.independent,
                stock_no="IND-NEW-001",
            ).exists()
        )

    # --------------------------------------------------
    # TENANT ISOLATION AND PRODUCT STATUS
    # --------------------------------------------------

    def test_product_from_another_company_returns_not_found(self):
        self.create_user(
            "product-tenant-owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        self.login("product-tenant-owner", "PRODUCT-HO")

        response = self.client.get(
            f"{self.list_url}{self.other_company_product.pk}/"
        )

        self.assertEqual(response.status_code, 404)

    def test_inactive_products_are_not_listed(self):
        self.head_office_product.is_active = False
        self.head_office_product.save(update_fields=["is_active"])

        self.create_user(
            "product-inactive-owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        self.login("product-inactive-owner", "PRODUCT-HO")

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200, response.data)

        returned_ids = {item["id"] for item in response.data}

        self.assertNotIn(
            self.head_office_product.id,
            returned_ids,
        )

    def test_delete_deactivates_product_and_preserves_stock_balance(self):
        self.create_user(
            "product-delete-owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        self.login("product-delete-owner", "PRODUCT-HO")

        balance = StockBalance.objects.create(
            product=self.head_office_product,
            business=self.head_office,
            quantity=Decimal("5.000"),
        )

        response = self.client.delete(
            f"{self.list_url}{self.head_office_product.pk}/"
        )

        self.assertEqual(response.status_code, 204)

        self.head_office_product.refresh_from_db()
        balance.refresh_from_db()

        self.assertFalse(self.head_office_product.is_active)
        self.assertEqual(balance.quantity, Decimal("5.000"))
        self.assertTrue(
            Product.objects.filter(
                pk=self.head_office_product.pk
            ).exists()
        )

    # --------------------------------------------------
    # STAFF MODULE PERMISSIONS
    # --------------------------------------------------

    def test_staff_can_view_catalog_with_view_permission(self):
        position = Position.objects.create(
            business=self.head_office,
            name="Product View Staff",
        )
        self.create_user(
            "product-staff-view",
            role=User.Role.STAFF,
            company=self.company,
            position=position,
        )
        PositionPermission.objects.create(
            position=position,
            module=PositionPermission.Module.INVENTORY,
            can_view=True,
        )
        self.login("product-staff-view", "PRODUCT-HO")

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200, response.data)

    def test_staff_without_view_permission_cannot_view_catalog(self):
        position = Position.objects.create(
            business=self.head_office,
            name="Product No View Staff",
        )
        self.create_user(
            "product-staff-no-view",
            role=User.Role.STAFF,
            company=self.company,
            position=position,
        )
        PositionPermission.objects.create(
            position=position,
            module=PositionPermission.Module.INVENTORY,
            can_create=True,
        )
        self.login("product-staff-no-view", "PRODUCT-HO")

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 403, response.data)

    def test_staff_can_create_product_with_create_permission(self):
        position = Position.objects.create(
            business=self.head_office,
            name="Product Create Staff",
        )
        self.create_user(
            "product-staff-create",
            role=User.Role.STAFF,
            company=self.company,
            position=position,
        )
        PositionPermission.objects.create(
            position=position,
            module=PositionPermission.Module.INVENTORY,
            can_create=True,
        )
        self.login("product-staff-create", "PRODUCT-HO")

        response = self.client.post(
            self.list_url,
            self.product_payload("STAFF-NEW-001"),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)

    def test_staff_cannot_create_product_without_create_permission(self):
        position = Position.objects.create(
            business=self.head_office,
            name="Product No Create Staff",
        )
        self.create_user(
            "product-staff-no-create",
            role=User.Role.STAFF,
            company=self.company,
            position=position,
        )
        PositionPermission.objects.create(
            position=position,
            module=PositionPermission.Module.INVENTORY,
            can_view=True,
        )
        self.login("product-staff-no-create", "PRODUCT-HO")

        response = self.client.post(
            self.list_url,
            self.product_payload("STAFF-DENIED-001"),
            format="json",
        )

        self.assertEqual(response.status_code, 403, response.data)

    # --------------------------------------------------
    # VALIDATION AND AUTHENTICATION
    # --------------------------------------------------

    def test_duplicate_stock_number_is_rejected(self):
        self.create_user(
            "product-duplicate-owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        self.login("product-duplicate-owner", "PRODUCT-HO")

        response = self.client.post(
            self.list_url,
            self.product_payload("HO-001", "Duplicate Product"),
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)

    def test_is_active_cannot_be_changed_through_regular_update(self):
        self.create_user(
            "product-active-owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        self.login("product-active-owner", "PRODUCT-HO")

        response = self.client.patch(
            f"{self.list_url}{self.head_office_product.pk}/",
            {"is_active": False},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)

        self.head_office_product.refresh_from_db()
        self.assertTrue(self.head_office_product.is_active)

    def test_unauthenticated_user_cannot_list_products(self):
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 401)
