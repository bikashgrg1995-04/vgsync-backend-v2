
from decimal import Decimal
from types import SimpleNamespace

from django.test import TestCase

from apps.businesses.models import Business
from apps.companies.models import Company
from apps.inventory.models import Product
from apps.inventory.serializers import StockMovementCreateSerializer


class StockMovementSerializerTests(TestCase):

    def setUp(self):
        self.company = Company.objects.create(
            name="Stock Serializer Test Company",
            code="STOCK-SER-CO",
        )

        self.head_office = Business.objects.create(
            name="Stock Serializer HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=self.company,
            business_code="STOCK-SER-HO",
        )

        self.branch = Business.objects.create(
            name="Stock Serializer Branch",
            business_type=Business.BusinessType.BRANCH,
            company=self.company,
            parent_business=self.head_office,
            business_code="STOCK-SER-BR",
        )

        self.product = Product.objects.create(
            owner_business=self.head_office,
            stock_no="STOCK-001",
            name="Test Stock Product",
            mrp=Decimal("1200.00"),
            purchase_price=Decimal("800.00"),
            sale_price=Decimal("1000.00"),
        )

    def make_serializer(self, payload, business=None):
        request = SimpleNamespace(
            active_business=business or self.head_office,
        )

        return StockMovementCreateSerializer(
            data=payload,
            context={"request": request},
        )

    def valid_payload(self, **overrides):
        payload = {
            "product_id": self.product.id,
            "movement_type": "purchase_receipt",
            "stock_condition": "good",
            "quantity": "10.500",
            "reference": "TEST-REF-001",
            "notes": "Test stock receipt",
        }
        payload.update(overrides)
        return payload

    # --------------------------------------------------
    # VALID PAYLOAD
    # --------------------------------------------------

    def test_valid_stock_movement_payload(self):
        serializer = self.make_serializer(self.valid_payload())

        self.assertTrue(serializer.is_valid(), serializer.errors)

        self.assertEqual(
            serializer.validated_data["product"],
            self.product,
        )
        self.assertEqual(
            serializer.validated_data["quantity"],
            Decimal("10.500"),
        )

    # --------------------------------------------------
    # BUSINESS PRODUCT ACCESS
    # --------------------------------------------------

    def test_branch_can_use_head_office_product(self):
        serializer = self.make_serializer(
            self.valid_payload(),
            business=self.branch,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data["product"],
            self.product,
        )

    def test_invalid_product_is_rejected(self):
        serializer = self.make_serializer(
            self.valid_payload(product_id=999999),
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("product_id", serializer.errors)

    # --------------------------------------------------
    # QUANTITY VALIDATION
    # --------------------------------------------------

    def test_integer_quantity_is_rejected(self):
        serializer = self.make_serializer(
            self.valid_payload(quantity=10),
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("quantity", serializer.errors)

    def test_float_quantity_is_rejected(self):
        serializer = self.make_serializer(
            self.valid_payload(quantity=10.5),
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("quantity", serializer.errors)

    def test_zero_quantity_is_rejected(self):
        serializer = self.make_serializer(
            self.valid_payload(quantity="0"),
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("quantity", serializer.errors)

    def test_negative_quantity_is_rejected(self):
        serializer = self.make_serializer(
            self.valid_payload(quantity="-5"),
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("quantity", serializer.errors)

    def test_more_than_three_decimal_places_is_rejected(self):
        serializer = self.make_serializer(
            self.valid_payload(quantity="1.1234"),
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("quantity", serializer.errors)

    # --------------------------------------------------
    # MOVEMENT TYPE VALIDATION
    # --------------------------------------------------

    def test_transfer_movement_type_is_rejected(self):
        serializer = self.make_serializer(
            self.valid_payload(movement_type="transfer_in"),
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("movement_type", serializer.errors)

    def test_reversal_movement_type_is_rejected(self):
        serializer = self.make_serializer(
            self.valid_payload(movement_type="reversal"),
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("movement_type", serializer.errors)

    # --------------------------------------------------
    # STOCK CONDITION VALIDATION
    # --------------------------------------------------

    def test_invalid_stock_condition_is_rejected(self):
        serializer = self.make_serializer(
            self.valid_payload(stock_condition="unknown"),
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("stock_condition", serializer.errors)
