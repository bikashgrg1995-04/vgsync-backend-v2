
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from apps.accounts.models import User
from apps.businesses.models import Business
from apps.companies.models import Company
from apps.inventory.models import (
    Product,
    StockBalance,
    StockMovement,
    StockTransferReturnReceipt,
)
from apps.inventory.services.transfers.orders import (
    create_stock_transfer_order,
)
from apps.inventory.services.transfers.dispatch import (
    dispatch_stock_transfer,
)
from apps.inventory.services.transfers.receipts import (
    receive_stock_transfer,
)
from apps.inventory.services.transfers.returns import (
    return_damaged_stock_transfer,
)
from apps.inventory.services.transfers.return_receipts import (
    receive_stock_transfer_return,
)


class StockTransferReturnReceiptServiceTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Return Receipt Test Company",
            code="RETURN-RECEIPT-CO",
        )

        cls.head_office = Business.objects.create(
            name="Return Receipt Test HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=cls.company,
            business_code="RETURN-RECEIPT-HO",
        )

        cls.branch = Business.objects.create(
            name="Return Receipt Test Branch",
            business_type=Business.BusinessType.BRANCH,
            company=cls.company,
            parent_business=cls.head_office,
            business_code="RETURN-RECEIPT-BR",
        )

        cls.product = Product.objects.create(
            owner_business=cls.head_office,
            stock_no="RETURN-RECEIPT-001",
            name="Return Receipt Product",
            purchase_price=Decimal("100.00"),
            sale_price=Decimal("150.00"),
        )

        cls.second_product = Product.objects.create(
            owner_business=cls.head_office,
            stock_no="RETURN-RECEIPT-002",
            name="Second Return Receipt Product",
            purchase_price=Decimal("200.00"),
            sale_price=Decimal("250.00"),
        )

        cls.branch_admin = User.objects.create_user(
            username="return-receipt-branch-admin",
            password="TestPass123!",
            role=User.Role.OWNER,
            business=cls.branch,
        )

        cls.company_owner = User.objects.create_user(
            username="return-receipt-company-owner",
            password="TestPass123!",
            role=User.Role.OWNER,
            company=cls.company,
        )

    def setUp(self):
        self.transfer = create_stock_transfer_order(
            user=self.branch_admin,
            branch=self.branch,
            items=[
                {
                    "product_id": self.product.pk,
                    "quantity": "10.000",
                },
            ],
            reference="RETURN-RECEIPT-ORDER-001",
        )

        self.transfer_item = self.transfer.items.get(
            product=self.product
        )

        self.head_office_balance = StockBalance.objects.create(
            product=self.product,
            business=self.head_office,
            quantity=Decimal("20.000"),
            damaged_quantity=Decimal("3.000"),
        )

        dispatch_stock_transfer(
            user=self.company_owner,
            transfer_id=self.transfer.pk,
            items=[
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "quantity": "5.000",
                },
            ],
            reference="RETURN-RECEIPT-DISPATCH-001",
        )

        receive_stock_transfer(
            user=self.branch_admin,
            transfer_id=self.transfer.pk,
            items=[
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "good_quantity": "3.000",
                    "damaged_quantity": "2.000",
                },
            ],
            reference="RETURN-RECEIPT-BRANCH-RECEIPT-001",
        )

        self.return_batch = return_damaged_stock_transfer(
            user=self.branch_admin,
            transfer_id=self.transfer.pk,
            items=[
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "quantity": "1.000",
                },
            ],
            reference="RETURN-RECEIPT-RETURN-001",
        )

        self.return_item = self.return_batch.items.get()

    # --------------------------------------------------
    # HELPERS
    # --------------------------------------------------

    def receive_return(self, **overrides):
        params = {
            "user": self.company_owner,
            "return_batch_id": self.return_batch.pk,
            "items": [
                {
                    "return_item_id": self.return_item.pk,
                    "received_quantity": "1.000",
                },
            ],
            "reference": "RETURN-RECEIPT-HO-001",
            "notes": "Damaged goods physically received.",
        }

        params.update(overrides)

        return receive_stock_transfer_return(**params)

    def get_head_office_balance(self):
        return StockBalance.objects.get(
            product=self.product,
            business=self.head_office,
        )

    def get_branch_balance(self):
        return StockBalance.objects.get(
            product=self.product,
            business=self.branch,
        )

    def create_second_product_stock(self):
        """
        Provide sufficient GOOD stock at HO for tests that dispatch
        the second product.
        """
        StockBalance.objects.create(
            product=self.second_product,
            business=self.head_office,
            quantity=Decimal("2.000"),
        )

    # --------------------------------------------------
    # SUCCESSFUL RECEIPT
    # --------------------------------------------------

    def test_head_office_can_receive_return_batch(self):
        receipt = self.receive_return()

        self.assertIsNotNone(receipt.pk)
        self.assertEqual(receipt.return_batch, self.return_batch)
        self.assertEqual(receipt.received_by, self.company_owner)

    def test_receipt_item_is_created(self):
        receipt = self.receive_return()

        receipt_item = receipt.items.get()

        self.assertEqual(receipt_item.return_item, self.return_item)
        self.assertEqual(
            receipt_item.received_quantity,
            Decimal("1.000"),
        )

    def test_receipt_increases_head_office_damaged_stock(self):
        self.receive_return()

        balance = self.get_head_office_balance()

        self.assertEqual(
            balance.damaged_quantity,
            Decimal("4.000"),
        )

    def test_receipt_does_not_change_head_office_good_stock(self):
        self.receive_return()

        balance = self.get_head_office_balance()

        self.assertEqual(
            balance.quantity,
            Decimal("15.000"),
        )

    def test_receipt_does_not_change_branch_stock(self):
        self.receive_return()

        balance = self.get_branch_balance()

        self.assertEqual(
            balance.quantity,
            Decimal("3.000"),
        )
        self.assertEqual(
            balance.damaged_quantity,
            Decimal("1.000"),
        )

    def test_receipt_creates_damaged_transfer_in_movement(self):
        receipt = self.receive_return()

        receipt_item = receipt.items.get()

        movement = StockMovement.objects.get(
            transfer_return_receipt_item=receipt_item,
        )

        self.assertEqual(
            movement.stock_condition,
            StockMovement.StockCondition.DAMAGED,
        )
        self.assertEqual(
            movement.movement_type,
            StockMovement.MovementType.TRANSFER_IN,
        )
        self.assertEqual(
            movement.quantity,
            Decimal("1.000"),
        )
        self.assertEqual(
            movement.balance_before,
            Decimal("3.000"),
        )
        self.assertEqual(
            movement.balance_after,
            Decimal("4.000"),
        )
        self.assertEqual(movement.created_by, self.company_owner)

    def test_movement_references_correct_receipt_item(self):
        receipt = self.receive_return()

        receipt_item = receipt.items.get()

        movement = StockMovement.objects.get(
            transfer_return_receipt_item=receipt_item,
        )

        self.assertEqual(
            movement.stock_balance.business,
            self.head_office,
        )
        self.assertEqual(
            movement.stock_balance.product,
            self.product,
        )
        self.assertEqual(
            movement.reference,
            receipt.reference,
        )

    # --------------------------------------------------
    # DUPLICATE RECEIPT
    # --------------------------------------------------

    def test_return_batch_can_only_be_received_once(self):
        self.receive_return()

        with self.assertRaises(ValidationError):
            self.receive_return(
                reference="RETURN-RECEIPT-DUPLICATE",
            )

        self.assertEqual(
            StockTransferReturnReceipt.objects.filter(
                return_batch=self.return_batch
            ).count(),
            1,
        )

        self.assertEqual(
            self.get_head_office_balance().damaged_quantity,
            Decimal("4.000"),
        )

    # --------------------------------------------------
    # COMPLETE RECEIPT VALIDATION
    # --------------------------------------------------

    def test_partial_receipt_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive_return(
                items=[
                    {
                        "return_item_id": self.return_item.pk,
                        "received_quantity": "0.500",
                    },
                ],
            )

        self.assertEqual(
            StockTransferReturnReceipt.objects.count(),
            0,
        )

        self.assertEqual(
            self.get_head_office_balance().damaged_quantity,
            Decimal("3.000"),
        )

    def test_excess_receipt_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive_return(
                items=[
                    {
                        "return_item_id": self.return_item.pk,
                        "received_quantity": "1.500",
                    },
                ],
            )

        self.assertEqual(
            StockTransferReturnReceipt.objects.count(),
            0,
        )

    def test_missing_return_item_is_rejected(self):
        transfer = create_stock_transfer_order(
            user=self.branch_admin,
            branch=self.branch,
            items=[
                {
                    "product_id": self.product.pk,
                    "quantity": "5.000",
                },
                {
                    "product_id": self.second_product.pk,
                    "quantity": "5.000",
                },
            ],
            reference="RETURN-RECEIPT-MISSING-ORDER",
        )

        transfer_items = {
            item.product_id: item
            for item in transfer.items.all()
        }

        # FIX: HO must have stock before dispatching the second product.
        self.create_second_product_stock()

        dispatch_stock_transfer(
            user=self.company_owner,
            transfer_id=transfer.pk,
            items=[
                {
                    "transfer_item_id": transfer_items[
                        self.product.pk
                    ].pk,
                    "quantity": "2.000",
                },
                {
                    "transfer_item_id": transfer_items[
                        self.second_product.pk
                    ].pk,
                    "quantity": "2.000",
                },
            ],
            reference="RETURN-RECEIPT-MISSING-DISPATCH",
        )

        receive_stock_transfer(
            user=self.branch_admin,
            transfer_id=transfer.pk,
            items=[
                {
                    "transfer_item_id": transfer_items[
                        self.product.pk
                    ].pk,
                    "good_quantity": "0",
                    "damaged_quantity": "1.000",
                },
                {
                    "transfer_item_id": transfer_items[
                        self.second_product.pk
                    ].pk,
                    "good_quantity": "0",
                    "damaged_quantity": "1.000",
                },
            ],
            reference="RETURN-RECEIPT-MISSING-BRANCH-RECEIPT",
        )

        return_batch = return_damaged_stock_transfer(
            user=self.branch_admin,
            transfer_id=transfer.pk,
            items=[
                {
                    "transfer_item_id": transfer_items[
                        self.product.pk
                    ].pk,
                    "quantity": "1.000",
                },
                {
                    "transfer_item_id": transfer_items[
                        self.second_product.pk
                    ].pk,
                    "quantity": "1.000",
                },
            ],
            reference="RETURN-RECEIPT-MISSING-RETURN",
        )

        return_item = return_batch.items.get(
            transfer_item=transfer_items[self.product.pk]
        )

        with self.assertRaises(ValidationError):
            receive_stock_transfer_return(
                user=self.company_owner,
                return_batch_id=return_batch.pk,
                items=[
                    {
                        "return_item_id": return_item.pk,
                        "received_quantity": "1.000",
                    },
                ],
                reference="RETURN-RECEIPT-MISSING-HO",
            )

        self.assertFalse(
            StockTransferReturnReceipt.objects.filter(
                return_batch=return_batch
            ).exists()
        )

    def test_item_from_another_return_batch_is_rejected(self):
        another_batch = return_damaged_stock_transfer(
            user=self.branch_admin,
            transfer_id=self.transfer.pk,
            items=[
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "quantity": "0.500",
                },
            ],
            reference="RETURN-RECEIPT-OTHER-RETURN",
        )

        another_item = another_batch.items.get()

        with self.assertRaises(ValidationError):
            self.receive_return(
                items=[
                    {
                        "return_item_id": another_item.pk,
                        "received_quantity": "0.500",
                    },
                ],
            )

        self.assertEqual(
            StockTransferReturnReceipt.objects.count(),
            0,
        )

    def test_duplicate_return_item_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive_return(
                items=[
                    {
                        "return_item_id": self.return_item.pk,
                        "received_quantity": "1.000",
                    },
                    {
                        "return_item_id": self.return_item.pk,
                        "received_quantity": "1.000",
                    },
                ],
            )

        self.assertEqual(
            StockTransferReturnReceipt.objects.count(),
            0,
        )

    # --------------------------------------------------
    # ACCESS VALIDATION
    # --------------------------------------------------

    def test_branch_admin_cannot_receive_at_head_office(self):
        with self.assertRaises(PermissionDenied):
            self.receive_return(user=self.branch_admin)

        self.assertEqual(
            StockTransferReturnReceipt.objects.count(),
            0,
        )

        self.assertEqual(
            self.get_head_office_balance().damaged_quantity,
            Decimal("3.000"),
        )

    # --------------------------------------------------
    # INPUT VALIDATION
    # --------------------------------------------------

    def test_invalid_return_batch_id_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive_return(return_batch_id=True)

    def test_empty_items_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive_return(items=[])

    def test_invalid_return_item_id_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive_return(
                items=[
                    {
                        "return_item_id": True,
                        "received_quantity": "1.000",
                    },
                ],
            )

    def test_float_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive_return(
                items=[
                    {
                        "return_item_id": self.return_item.pk,
                        "received_quantity": 1.0,
                    },
                ],
            )

    def test_integer_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive_return(
                items=[
                    {
                        "return_item_id": self.return_item.pk,
                        "received_quantity": 1,
                    },
                ],
            )

    def test_boolean_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive_return(
                items=[
                    {
                        "return_item_id": self.return_item.pk,
                        "received_quantity": True,
                    },
                ],
            )

    def test_more_than_three_decimal_places_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive_return(
                items=[
                    {
                        "return_item_id": self.return_item.pk,
                        "received_quantity": "1.0001",
                    },
                ],
            )

    def test_invalid_reference_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive_return(reference=" ")

    def test_reference_over_100_characters_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive_return(reference="R" * 101)

    def test_non_string_notes_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive_return(notes=123)

    # --------------------------------------------------
    # TRANSACTION ROLLBACK
    # --------------------------------------------------

    def test_invalid_later_item_rolls_back_receipt(self):
        transfer = create_stock_transfer_order(
            user=self.branch_admin,
            branch=self.branch,
            items=[
                {
                    "product_id": self.product.pk,
                    "quantity": "5.000",
                },
                {
                    "product_id": self.second_product.pk,
                    "quantity": "5.000",
                },
            ],
            reference="RETURN-RECEIPT-ROLLBACK-ORDER",
        )

        transfer_items = {
            item.product_id: item
            for item in transfer.items.all()
        }

        # FIX: HO must have stock before dispatching the second product.
        self.create_second_product_stock()

        dispatch_stock_transfer(
            user=self.company_owner,
            transfer_id=transfer.pk,
            items=[
                {
                    "transfer_item_id": transfer_items[
                        self.product.pk
                    ].pk,
                    "quantity": "2.000",
                },
                {
                    "transfer_item_id": transfer_items[
                        self.second_product.pk
                    ].pk,
                    "quantity": "2.000",
                },
            ],
            reference="RETURN-RECEIPT-ROLLBACK-DISPATCH",
        )

        receive_stock_transfer(
            user=self.branch_admin,
            transfer_id=transfer.pk,
            items=[
                {
                    "transfer_item_id": transfer_items[
                        self.product.pk
                    ].pk,
                    "good_quantity": "0",
                    "damaged_quantity": "1.000",
                },
                {
                    "transfer_item_id": transfer_items[
                        self.second_product.pk
                    ].pk,
                    "good_quantity": "0",
                    "damaged_quantity": "1.000",
                },
            ],
            reference="RETURN-RECEIPT-ROLLBACK-BRANCH-RECEIPT",
        )

        return_batch = return_damaged_stock_transfer(
            user=self.branch_admin,
            transfer_id=transfer.pk,
            items=[
                {
                    "transfer_item_id": transfer_items[
                        self.product.pk
                    ].pk,
                    "quantity": "1.000",
                },
                {
                    "transfer_item_id": transfer_items[
                        self.second_product.pk
                    ].pk,
                    "quantity": "1.000",
                },
            ],
            reference="RETURN-RECEIPT-ROLLBACK-RETURN",
        )

        return_items = {
            item.transfer_item.product_id: item
            for item in return_batch.items.select_related(
                "transfer_item"
            )
        }

        with self.assertRaises(ValidationError):
            receive_stock_transfer_return(
                user=self.company_owner,
                return_batch_id=return_batch.pk,
                items=[
                    {
                        "return_item_id": return_items[
                            self.product.pk
                        ].pk,
                        "received_quantity": "1.000",
                    },
                    {
                        "return_item_id": return_items[
                            self.second_product.pk
                        ].pk,
                        "received_quantity": "0.500",
                    },
                ],
                reference="RETURN-RECEIPT-ROLLBACK-HO",
            )

        self.assertFalse(
            StockTransferReturnReceipt.objects.filter(
                return_batch=return_batch
            ).exists()
        )

        self.assertFalse(
            StockMovement.objects.filter(
                transfer_return_receipt_item__isnull=False
            ).exists()
        )

        self.assertEqual(
            self.get_head_office_balance().damaged_quantity,
            Decimal("3.000"),
        )

    # --------------------------------------------------
    # IMMUTABLE HISTORY
    # --------------------------------------------------

    def test_receipt_event_cannot_be_updated_or_deleted(self):
        receipt = self.receive_return()

        with self.assertRaises(ValidationError):
            receipt.reference = "CHANGED-RECEIPT"
            receipt.save()

        with self.assertRaises(ValidationError):
            receipt.delete()

    def test_receipt_item_cannot_be_updated_or_deleted(self):
        receipt = self.receive_return()
        receipt_item = receipt.items.get()

        with self.assertRaises(ValidationError):
            receipt_item.received_quantity = Decimal("0.500")
            receipt_item.save()

        with self.assertRaises(ValidationError):
            receipt_item.delete()
