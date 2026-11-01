
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from apps.accounts.models import User
from apps.businesses.models import Business
from apps.companies.models import Company
from apps.inventory.models import (
    LocalProduct,
    Product,
    StockBalance,
    StockMovement,
)
from apps.inventory.services.validators import (
    validate_inventory_access,
    validate_product_business,
)


class InventoryModelTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Test Company",
            code="TEST-CO",
        )

        cls.head_office = Business.objects.create(
            name="Test Head Office",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=cls.company,
            business_code="TEST-HO",
        )

        cls.branch = Business.objects.create(
            name="Test Branch",
            business_type=Business.BusinessType.BRANCH,
            company=cls.company,
            parent_business=cls.head_office,
            business_code="TEST-BR",
        )

        cls.other_company = Company.objects.create(
            name="Other Company",
            code="OTHER-CO",
        )

        cls.other_head_office = Business.objects.create(
            name="Other Head Office",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=cls.other_company,
            business_code="OTHER-HO",
        )

        cls.other_branch = Business.objects.create(
            name="Other Branch",
            business_type=Business.BusinessType.BRANCH,
            company=cls.other_company,
            parent_business=cls.other_head_office,
            business_code="OTHER-BR",
        )

        cls.independent_business = Business.objects.create(
            name="Independent Business",
            business_type=Business.BusinessType.INDEPENDENT,
            business_code="INDEPENDENT-01",
        )

        cls.other_independent_business = Business.objects.create(
            name="Other Independent Business",
            business_type=Business.BusinessType.INDEPENDENT,
            business_code="INDEPENDENT-02",
        )

        cls.product = Product.objects.create(
            owner_business=cls.head_office,
            stock_no="SP-001",
            name="Test Product",
            mrp=Decimal("1200.00"),
            purchase_price=Decimal("800.00"),
            sale_price=Decimal("1000.00"),
        )

        cls.independent_product = Product.objects.create(
            owner_business=cls.independent_business,
            stock_no="IND-001",
            name="Independent Product",
            mrp=Decimal("500.00"),
            purchase_price=Decimal("300.00"),
            sale_price=Decimal("400.00"),
        )

    # --------------------------------------------------
    # PRODUCT MODEL TESTS
    # --------------------------------------------------

    def test_head_office_can_own_central_product(self):
        self.product.full_clean()

    def test_branch_cannot_own_central_product(self):
        product = Product(
            owner_business=self.branch,
            stock_no="BR-001",
            name="Invalid Branch Product",
        )

        with self.assertRaises(ValidationError):
            product.full_clean()

    # --------------------------------------------------
    # STOCK BALANCE MODEL TESTS
    # --------------------------------------------------

    def test_head_office_can_hold_its_own_stock(self):
        balance = StockBalance(
            product=self.product,
            business=self.head_office,
            quantity=Decimal("100.000"),
        )

        balance.full_clean()

    def test_branch_can_hold_its_head_office_product(self):
        balance = StockBalance(
            product=self.product,
            business=self.branch,
            quantity=Decimal("25.500"),
        )

        balance.full_clean()

    def test_branch_cannot_hold_another_company_product(self):
        balance = StockBalance(
            product=self.product,
            business=self.other_branch,
            quantity=Decimal("10.000"),
        )

        with self.assertRaises(ValidationError):
            balance.full_clean()

    def test_independent_business_can_hold_its_own_product(self):
        balance = StockBalance(
            product=self.independent_product,
            business=self.independent_business,
            quantity=Decimal("50.250"),
        )

        balance.full_clean()

    def test_independent_business_cannot_hold_another_business_product(self):
        balance = StockBalance(
            product=self.independent_product,
            business=self.other_independent_business,
            quantity=Decimal("10.000"),
        )

        with self.assertRaises(ValidationError):
            balance.full_clean()

    def test_stock_quantity_supports_three_decimal_places(self):
        balance = StockBalance.objects.create(
            product=self.product,
            business=self.branch,
            quantity=Decimal("2.750"),
        )

        self.assertEqual(balance.quantity, Decimal("2.750"))

    def test_negative_stock_quantity_is_rejected(self):
        balance = StockBalance(
            product=self.product,
            business=self.branch,
            quantity=Decimal("-1.000"),
        )

        with self.assertRaises(ValidationError):
            balance.full_clean()

    # --------------------------------------------------
    # LOCAL PRODUCT MODEL TESTS
    # --------------------------------------------------

    def test_local_product_has_separate_purchase_and_sale_prices(self):
        local_product = LocalProduct.objects.create(
            business=self.branch,
            name="Local Item",
            purchase_price=Decimal("800.00"),
            sale_price=Decimal("1000.00"),
        )

        self.assertEqual(
            local_product.purchase_price,
            Decimal("800.00"),
        )
        self.assertEqual(
            local_product.sale_price,
            Decimal("1000.00"),
        )

    def test_local_product_rejects_negative_purchase_price(self):
        local_product = LocalProduct(
            business=self.branch,
            name="Invalid Local Item",
            purchase_price=Decimal("-100.00"),
            sale_price=Decimal("200.00"),
        )

        with self.assertRaises(ValidationError):
            local_product.full_clean()

    # --------------------------------------------------
    # STOCK MOVEMENT MODEL TESTS
    # --------------------------------------------------

    def test_stock_movement_requires_positive_quantity(self):
        balance = StockBalance.objects.create(
            product=self.product,
            business=self.branch,
            quantity=Decimal("10.000"),
        )

        movement = StockMovement(
            stock_balance=balance,
            movement_type=StockMovement.MovementType.OPENING,
            quantity=Decimal("0.000"),
            balance_before=Decimal("0.000"),
            balance_after=Decimal("0.000"),
        )

        with self.assertRaises(ValidationError):
            movement.full_clean()

    # --------------------------------------------------
    # PRODUCT-BUSINESS VALIDATOR TESTS
    # --------------------------------------------------

    def test_head_office_can_use_its_own_product(self):
        result = validate_product_business(
            self.product,
            self.head_office,
        )

        self.assertTrue(result)

    def test_branch_can_use_head_office_product(self):
        result = validate_product_business(
            self.product,
            self.branch,
        )

        self.assertTrue(result)

    def test_branch_cannot_use_another_company_product(self):
        with self.assertRaises(ValidationError):
            validate_product_business(
                self.product,
                self.other_branch,
            )

    def test_independent_business_can_use_its_own_product(self):
        result = validate_product_business(
            self.independent_product,
            self.independent_business,
        )

        self.assertTrue(result)

    def test_independent_business_cannot_use_another_business_product(self):
        with self.assertRaises(ValidationError):
            validate_product_business(
                self.independent_product,
                self.other_independent_business,
            )

    def test_inactive_product_cannot_be_used(self):
        self.product.is_active = False
        self.product.save(update_fields=["is_active"])

        with self.assertRaises(ValidationError):
            validate_product_business(
                self.product,
                self.branch,
            )

    def test_inactive_product_can_be_used_when_active_check_is_disabled(self):
        self.product.is_active = False
        self.product.save(update_fields=["is_active"])

        result = validate_product_business(
            self.product,
            self.branch,
            require_active=False,
        )

        self.assertTrue(result)


class InventoryAccessValidatorTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Access Test Company",
            code="ACCESS-CO",
        )

        cls.head_office = Business.objects.create(
            name="Access Test HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=cls.company,
            business_code="ACCESS-HO",
        )

        cls.branch = Business.objects.create(
            name="Access Test Branch",
            business_type=Business.BusinessType.BRANCH,
            company=cls.company,
            parent_business=cls.head_office,
            business_code="ACCESS-BR",
        )

        cls.other_company = Company.objects.create(
            name="Other Access Company",
            code="OTHER-ACCESS",
        )

        cls.other_head_office = Business.objects.create(
            name="Other Access HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=cls.other_company,
            business_code="OTHER-ACCESS-HO",
        )

        cls.independent = Business.objects.create(
            name="Access Independent",
            business_type=Business.BusinessType.INDEPENDENT,
            business_code="ACCESS-IND",
        )

    def make_user(self, username, **kwargs):
        return User.objects.create_user(
            username=username,
            password="TestPass123!",
            **kwargs,
        )

    # --------------------------------------------------
    # COMPANY ACCESS TESTS
    # --------------------------------------------------

    def test_company_owner_has_full_inventory_access_to_head_office(self):
        user = self.make_user(
            "inventory-owner",
            role=User.Role.OWNER,
            company=self.company,
        )

        result = validate_inventory_access(
            user,
            self.head_office,
            write=True,
        )

        self.assertEqual(result, "full")

    def test_company_owner_has_read_only_inventory_access_to_branch(self):
        user = self.make_user(
            "inventory-company-owner",
            role=User.Role.OWNER,
            company=self.company,
        )

        result = validate_inventory_access(
            user,
            self.branch,
        )

        self.assertEqual(result, "read_only")

    def test_company_owner_cannot_write_to_branch_inventory(self):
        user = self.make_user(
            "inventory-readonly-owner",
            role=User.Role.OWNER,
            company=self.company,
        )

        with self.assertRaises(PermissionDenied):
            validate_inventory_access(
                user,
                self.branch,
                write=True,
            )

    def test_user_cannot_access_another_company_inventory(self):
        user = self.make_user(
            "inventory-company-user",
            role=User.Role.OWNER,
            company=self.company,
        )

        with self.assertRaises(PermissionDenied):
            validate_inventory_access(
                user,
                self.other_head_office,
            )

    # --------------------------------------------------
    # BUSINESS ACCESS TESTS
    # --------------------------------------------------

    def test_branch_admin_has_full_inventory_access_to_own_branch(self):
        user = self.make_user(
            "inventory-branch-admin",
            role=User.Role.ADMIN,
            business=self.branch,
        )

        result = validate_inventory_access(
            user,
            self.branch,
            write=True,
        )

        self.assertEqual(result, "full")

    def test_independent_owner_has_full_inventory_access(self):
        user = self.make_user(
            "inventory-independent-owner",
            role=User.Role.OWNER,
            business=self.independent,
        )

        result = validate_inventory_access(
            user,
            self.independent,
            write=True,
        )

        self.assertEqual(result, "full")

    # --------------------------------------------------
    # ACCOUNT STATE TESTS
    # --------------------------------------------------

    def test_inactive_user_cannot_access_inventory(self):
        user = self.make_user(
            "inventory-inactive-user",
            role=User.Role.OWNER,
            company=self.company,
            is_active=False,
        )

        with self.assertRaises(PermissionDenied):
            validate_inventory_access(
                user,
                self.head_office,
            )
