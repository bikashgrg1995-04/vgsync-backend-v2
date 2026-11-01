
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.accounts.models import User
from apps.businesses.models import Business
from apps.companies.models import Company
from apps.inventory.models import Product, StockBalance, StockMovement
from apps.inventory.services.stock import (
    create_stock_movement,
    validate_quantity,
)


class StockServiceTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Stock Test Company",
            code="STOCK-TEST",
        )

        cls.head_office = Business.objects.create(
            name="Stock Test HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=cls.company,
            business_code="STOCK-HO",
        )

        cls.user = User.objects.create_user(
            username="stock-owner",
            password="TestPass123!",
            role=User.Role.OWNER,
            company=cls.company,
        )

        cls.product = Product.objects.create(
            owner_business=cls.head_office,
            stock_no="STOCK-001",
            name="Stock Test Product",
            purchase_price=Decimal("100.00"),
            sale_price=Decimal("150.00"),
        )

    def create_movement(self, movement_type, quantity):
        return create_stock_movement(
            user=self.user,
            business=self.head_office,
            product=self.product,
            movement_type=movement_type,
            quantity=quantity,
        )

    # --------------------------------------------------
    # STOCK MOVEMENT TESTS
    # --------------------------------------------------

    def test_opening_stock_creates_balance_and_movement(self):
        movement = self.create_movement(
            StockMovement.MovementType.OPENING,
            "100.000",
        )

        balance = StockBalance.objects.get(
            product=self.product,
            business=self.head_office,
        )

        self.assertEqual(balance.quantity, Decimal("100.000"))
        self.assertEqual(movement.balance_before, Decimal("0.000"))
        self.assertEqual(movement.balance_after, Decimal("100.000"))

    def test_purchase_receipt_increases_stock(self):
        self.create_movement(
            StockMovement.MovementType.OPENING,
            "100.000",
        )

        self.create_movement(
            StockMovement.MovementType.PURCHASE_RECEIPT,
            "25.500",
        )

        balance = StockBalance.objects.get(
            product=self.product,
            business=self.head_office,
        )

        self.assertEqual(balance.quantity, Decimal("125.500"))

    def test_sale_issue_decreases_stock(self):
        self.create_movement(
            StockMovement.MovementType.OPENING,
            "100.000",
        )

        movement = self.create_movement(
            StockMovement.MovementType.SALE_ISSUE,
            "30.000",
        )

        balance = StockBalance.objects.get(
            product=self.product,
            business=self.head_office,
        )

        self.assertEqual(balance.quantity, Decimal("70.000"))
        self.assertEqual(movement.balance_before, Decimal("100.000"))
        self.assertEqual(movement.balance_after, Decimal("70.000"))

    def test_insufficient_stock_is_rejected(self):
        self.create_movement(
            StockMovement.MovementType.OPENING,
            "10.000",
        )

        with self.assertRaises(ValidationError):
            self.create_movement(
                StockMovement.MovementType.SALE_ISSUE,
                "15.000",
            )

        balance = StockBalance.objects.get(
            product=self.product,
            business=self.head_office,
        )

        self.assertEqual(balance.quantity, Decimal("10.000"))
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_reversal_is_not_allowed_in_generic_service(self):
        with self.assertRaises(ValidationError):
            self.create_movement(
                StockMovement.MovementType.REVERSAL,
                "5.000",
            )

    def test_failed_movement_does_not_create_balance(self):
        with self.assertRaises(ValidationError):
            self.create_movement(
                StockMovement.MovementType.SALE_ISSUE,
                "5.000",
            )

        self.assertFalse(
            StockBalance.objects.filter(
                product=self.product,
                business=self.head_office,
            ).exists()
        )

        self.assertEqual(StockMovement.objects.count(), 0)

    # --------------------------------------------------
    # QUANTITY VALIDATION TESTS
    # --------------------------------------------------

    def test_float_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_quantity(10.5)

    def test_zero_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_quantity("0")

    def test_negative_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_quantity("-5")

    def test_quantity_with_more_than_three_decimal_places_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_quantity("1.2345")

    def test_valid_decimal_quantity_is_accepted(self):
        result = validate_quantity("12.345")

        self.assertEqual(result, Decimal("12.345"))

    # --------------------------------------------------
    # STOCK MOVEMENT IMMUTABILITY TESTS
    # --------------------------------------------------

    def test_existing_stock_movement_cannot_be_updated(self):
        movement = self.create_movement(
            StockMovement.MovementType.OPENING,
            "10.000",
        )

        movement.notes = "Changed history"

        with self.assertRaises(ValidationError):
            movement.save()

        movement.refresh_from_db()

        self.assertEqual(movement.notes, "")

    def test_stock_movement_cannot_be_deleted(self):
        movement = self.create_movement(
            StockMovement.MovementType.OPENING,
            "10.000",
        )

        with self.assertRaises(ValidationError):
            movement.delete()

        self.assertTrue(
            StockMovement.objects.filter(pk=movement.pk).exists()
        )

    def test_stock_movement_queryset_cannot_be_updated(self):
        self.create_movement(
            StockMovement.MovementType.OPENING,
            "10.000",
        )

        with self.assertRaises(ValidationError):
            StockMovement.objects.all().update(
                notes="Changed history"
            )

    def test_stock_movement_queryset_cannot_be_deleted(self):
        self.create_movement(
            StockMovement.MovementType.OPENING,
            "10.000",
        )

        with self.assertRaises(ValidationError):
            StockMovement.objects.all().delete()

        self.assertEqual(StockMovement.objects.count(), 1)
