
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from apps.accounts.models import User
from apps.businesses.models import Business
from apps.companies.models import Company
from apps.inventory.models import (
    Product,
    StockBalance,
    StockTransfer,
    StockTransferItem,
)
from apps.inventory.services.transfers.orders import (
    create_stock_transfer_order,
)


class StockTransferOrderServiceTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Transfer Test Company",
            code="TRANSFER-CO",
        )

        cls.head_office = Business.objects.create(
            name="Transfer Test HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=cls.company,
            business_code="TRANSFER-HO",
        )

        cls.branch = Business.objects.create(
            name="Transfer Test Branch",
            business_type=Business.BusinessType.BRANCH,
            company=cls.company,
            parent_business=cls.head_office,
            business_code="TRANSFER-BR",
        )

        cls.independent = Business.objects.create(
            name="Transfer Independent",
            business_type=Business.BusinessType.INDEPENDENT,
            business_code="TRANSFER-IND",
        )

        cls.other_company = Company.objects.create(
            name="Other Transfer Company",
            code="OTHER-TRANSFER-CO",
        )

        cls.other_head_office = Business.objects.create(
            name="Other Transfer HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=cls.other_company,
            business_code="OTHER-TRANSFER-HO",
        )

        cls.product = Product.objects.create(
            owner_business=cls.head_office,
            stock_no="TRANSFER-001",
            name="Transfer Product",
            purchase_price=Decimal("100.00"),
            sale_price=Decimal("150.00"),
        )

        cls.second_product = Product.objects.create(
            owner_business=cls.head_office,
            stock_no="TRANSFER-002",
            name="Second Transfer Product",
            purchase_price=Decimal("200.00"),
            sale_price=Decimal("250.00"),
        )

        cls.other_product = Product.objects.create(
            owner_business=cls.other_head_office,
            stock_no="OTHER-001",
            name="Other Company Product",
            purchase_price=Decimal("100.00"),
            sale_price=Decimal("150.00"),
        )

        cls.branch_admin = User.objects.create_user(
            username="transfer-branch-admin",
            password="TestPass123!",
            role=User.Role.OWNER,
            business=cls.branch,
        )

        cls.company_owner = User.objects.create_user(
            username="transfer-company-owner",
            password="TestPass123!",
            role=User.Role.OWNER,
            company=cls.company,
        )

        cls.independent_owner = User.objects.create_user(
            username="transfer-independent-owner",
            password="TestPass123!",
            role=User.Role.OWNER,
            business=cls.independent,
        )

    def create_order(self, **overrides):
        params = {
            "user": self.branch_admin,
            "branch": self.branch,
            "items": [
                {
                    "product_id": self.product.pk,
                    "quantity": "10.000",
                },
            ],
            "reference": "TRANSFER-ORDER-001",
            "notes": "Test transfer order",
        }
        params.update(overrides)

        return create_stock_transfer_order(**params)

    # --------------------------------------------------
    # SUCCESSFUL ORDER CREATION
    # --------------------------------------------------

    def test_branch_can_create_order_for_its_head_office(self):
        transfer = self.create_order()

        self.assertEqual(
            transfer.source_business,
            self.head_office,
        )
        self.assertEqual(
            transfer.destination_business,
            self.branch,
        )
        self.assertEqual(
            transfer.status,
            StockTransfer.Status.ORDERED,
        )
        self.assertEqual(
            transfer.created_by,
            self.branch_admin,
        )

    def test_order_items_are_created_correctly(self):
        transfer = self.create_order(
            items=[
                {
                    "product_id": self.product.pk,
                    "quantity": "10.000",
                },
                {
                    "product_id": self.second_product.pk,
                    "quantity": "5.500",
                },
            ]
        )

        items = list(
            StockTransferItem.objects.filter(
                transfer=transfer
            ).order_by("product_id")
        )

        self.assertEqual(len(items), 2)
        self.assertEqual(
            items[0].ordered_quantity,
            Decimal("10.000"),
        )
        self.assertEqual(
            items[1].ordered_quantity,
            Decimal("5.500"),
        )

    def test_order_creation_does_not_change_stock(self):
        StockBalance.objects.create(
            product=self.product,
            business=self.head_office,
            quantity=Decimal("100.000"),
        )

        StockBalance.objects.create(
            product=self.product,
            business=self.branch,
            quantity=Decimal("20.000"),
        )

        self.create_order()

        head_office_balance = StockBalance.objects.get(
            product=self.product,
            business=self.head_office,
        )

        branch_balance = StockBalance.objects.get(
            product=self.product,
            business=self.branch,
        )

        self.assertEqual(
            head_office_balance.quantity,
            Decimal("100.000"),
        )
        self.assertEqual(
            branch_balance.quantity,
            Decimal("20.000"),
        )

    # --------------------------------------------------
    # BUSINESS ACCESS VALIDATION
    # --------------------------------------------------

    def test_independent_business_cannot_create_transfer_order(self):
        with self.assertRaises(ValidationError):
            self.create_order(
                user=self.independent_owner,
                branch=self.independent,
            )

        self.assertEqual(StockTransfer.objects.count(), 0)

    def test_company_user_cannot_create_order_from_branch(self):
        with self.assertRaises(PermissionDenied):
            self.create_order(
                user=self.company_owner,
            )

        self.assertEqual(StockTransfer.objects.count(), 0)

    # --------------------------------------------------
    # PRODUCT VALIDATION
    # --------------------------------------------------

    def test_product_from_another_head_office_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_order(
                items=[
                    {
                        "product_id": self.other_product.pk,
                        "quantity": "5.000",
                    },
                ]
            )

        self.assertEqual(StockTransfer.objects.count(), 0)

    def test_inactive_product_is_rejected(self):
        self.product.is_active = False
        self.product.save(update_fields=["is_active"])

        with self.assertRaises(ValidationError):
            self.create_order()

        self.assertEqual(StockTransfer.objects.count(), 0)

    def test_nonexistent_product_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_order(
                items=[
                    {
                        "product_id": 999999,
                        "quantity": "5.000",
                    },
                ]
            )

        self.assertEqual(StockTransfer.objects.count(), 0)

    def test_duplicate_product_in_order_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_order(
                items=[
                    {
                        "product_id": self.product.pk,
                        "quantity": "5.000",
                    },
                    {
                        "product_id": self.product.pk,
                        "quantity": "3.000",
                    },
                ]
            )

        self.assertEqual(StockTransfer.objects.count(), 0)

    # --------------------------------------------------
    # QUANTITY VALIDATION
    # --------------------------------------------------

    def test_zero_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_order(
                items=[
                    {
                        "product_id": self.product.pk,
                        "quantity": "0",
                    },
                ]
            )

        self.assertEqual(StockTransfer.objects.count(), 0)

    def test_negative_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_order(
                items=[
                    {
                        "product_id": self.product.pk,
                        "quantity": "-5",
                    },
                ]
            )

        self.assertEqual(StockTransfer.objects.count(), 0)

    def test_float_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_order(
                items=[
                    {
                        "product_id": self.product.pk,
                        "quantity": 10.5,
                    },
                ]
            )

        self.assertEqual(StockTransfer.objects.count(), 0)

    def test_more_than_three_decimal_places_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_order(
                items=[
                    {
                        "product_id": self.product.pk,
                        "quantity": "1.2345",
                    },
                ]
            )

        self.assertEqual(StockTransfer.objects.count(), 0)

    # --------------------------------------------------
    # ORDER VALIDATION
    # --------------------------------------------------

    def test_empty_order_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_order(items=[])

        self.assertEqual(StockTransfer.objects.count(), 0)

    def test_missing_reference_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_order(reference="   ")

        self.assertEqual(StockTransfer.objects.count(), 0)

    def test_invalid_item_structure_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_order(
                items=["invalid-item"]
            )

        self.assertEqual(StockTransfer.objects.count(), 0)

    def test_invalid_later_item_rolls_back_entire_order(self):
        with self.assertRaises(ValidationError):
            self.create_order(
                items=[
                    {
                        "product_id": self.product.pk,
                        "quantity": "10.000",
                    },
                    {
                        "product_id": self.other_product.pk,
                        "quantity": "5.000",
                    },
                ]
            )

        self.assertEqual(StockTransfer.objects.count(), 0)
        self.assertEqual(StockTransferItem.objects.count(), 0)
