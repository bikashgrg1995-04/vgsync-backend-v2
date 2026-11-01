
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
    StockTransfer,
    StockTransferDispatch,
    StockTransferReceipt,
    StockTransferReceiptItem,
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


class StockTransferReceiptServiceTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Receipt Test Company",
            code="RECEIPT-CO",
        )

        cls.head_office = Business.objects.create(
            name="Receipt Test HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=cls.company,
            business_code="RECEIPT-HO",
        )

        cls.branch = Business.objects.create(
            name="Receipt Test Branch",
            business_type=Business.BusinessType.BRANCH,
            company=cls.company,
            parent_business=cls.head_office,
            business_code="RECEIPT-BR",
        )

        cls.product = Product.objects.create(
            owner_business=cls.head_office,
            stock_no="RECEIPT-001",
            name="Receipt Product",
            purchase_price=Decimal("100.00"),
            sale_price=Decimal("150.00"),
        )

        cls.second_product = Product.objects.create(
            owner_business=cls.head_office,
            stock_no="RECEIPT-002",
            name="Second Receipt Product",
            purchase_price=Decimal("200.00"),
            sale_price=Decimal("250.00"),
        )

        cls.branch_admin = User.objects.create_user(
            username="receipt-branch-admin",
            password="TestPass123!",
            role=User.Role.ADMIN,
            business=cls.branch,
        )

        cls.company_owner = User.objects.create_user(
            username="receipt-company-owner",
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
            reference="RECEIPT-ORDER-001",
            notes="Receipt test order",
        )

        self.head_office_balance = StockBalance.objects.create(
            product=self.product,
            business=self.head_office,
            quantity=Decimal("20.000"),
            damaged_quantity=Decimal("3.000"),
        )

        self.transfer_item = self.transfer.items.get(
            product=self.product
        )

        self.dispatch(
            quantity="5.000",
            reference="RECEIPT-DISPATCH-001",
        )

    def dispatch(
        self,
        *,
        quantity,
        reference,
        transfer=None,
        transfer_item=None,
        user=None,
    ):
        transfer = transfer or self.transfer
        transfer_item = transfer_item or transfer.items.get(
            product=self.product
        )

        return dispatch_stock_transfer(
            user=user or self.company_owner,
            transfer_id=transfer.pk,
            items=[
                {
                    "transfer_item_id": transfer_item.pk,
                    "quantity": quantity,
                },
            ],
            reference=reference,
        )

    def receive(self, **overrides):
        params = {
            "user": self.branch_admin,
            "transfer_id": self.transfer.pk,
            "items": [
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "good_quantity": "3.000",
                    "damaged_quantity": "2.000",
                },
            ],
            "reference": "RECEIPT-001",
            "notes": "Test receipt",
        }

        params.update(overrides)

        return receive_stock_transfer(**params)

    def get_branch_balance(self, product=None):
        return StockBalance.objects.get(
            product=product or self.product,
            business=self.branch,
        )

    # --------------------------------------------------
    # SUCCESSFUL RECEIPT
    # --------------------------------------------------

    def test_branch_can_receive_dispatched_stock(self):
        receipt = self.receive()

        self.assertIsNotNone(receipt.pk)
        self.assertEqual(receipt.transfer, self.transfer)
        self.assertEqual(receipt.received_by, self.branch_admin)

    def test_receipt_creates_receipt_item(self):
        receipt = self.receive()

        receipt_item = StockTransferReceiptItem.objects.get(
            receipt=receipt,
        )

        self.assertEqual(
            receipt_item.transfer_item,
            self.transfer_item,
        )
        self.assertEqual(
            receipt_item.good_quantity,
            Decimal("3.000"),
        )
        self.assertEqual(
            receipt_item.damaged_quantity,
            Decimal("2.000"),
        )

    def test_good_quantity_increases_branch_good_stock(self):
        self.receive()

        balance = self.get_branch_balance()

        self.assertEqual(
            balance.quantity,
            Decimal("3.000"),
        )

    def test_damaged_quantity_increases_branch_damaged_stock(self):
        self.receive()

        balance = self.get_branch_balance()

        self.assertEqual(
            balance.damaged_quantity,
            Decimal("2.000"),
        )

    def test_receipt_does_not_change_head_office_stock(self):
        self.receive()

        self.head_office_balance.refresh_from_db()

        self.assertEqual(
            self.head_office_balance.quantity,
            Decimal("15.000"),
        )
        self.assertEqual(
            self.head_office_balance.damaged_quantity,
            Decimal("3.000"),
        )

    def test_good_receipt_creates_good_transfer_in_movement(self):
        receipt = self.receive()

        receipt_item = StockTransferReceiptItem.objects.get(
            receipt=receipt,
        )

        movement = StockMovement.objects.get(
            transfer_receipt_item=receipt_item,
            stock_condition=StockMovement.StockCondition.GOOD,
        )

        self.assertEqual(
            movement.movement_type,
            StockMovement.MovementType.TRANSFER_IN,
        )
        self.assertEqual(
            movement.quantity,
            Decimal("3.000"),
        )
        self.assertEqual(
            movement.balance_before,
            Decimal("0.000"),
        )
        self.assertEqual(
            movement.balance_after,
            Decimal("3.000"),
        )
        self.assertEqual(
            movement.created_by,
            self.branch_admin,
        )

    def test_damaged_receipt_creates_damaged_transfer_in_movement(self):
        receipt = self.receive()

        receipt_item = StockTransferReceiptItem.objects.get(
            receipt=receipt,
        )

        movement = StockMovement.objects.get(
            transfer_receipt_item=receipt_item,
            stock_condition=StockMovement.StockCondition.DAMAGED,
        )

        self.assertEqual(
            movement.movement_type,
            StockMovement.MovementType.TRANSFER_IN,
        )
        self.assertEqual(
            movement.quantity,
            Decimal("2.000"),
        )
        self.assertEqual(
            movement.balance_before,
            Decimal("0.000"),
        )
        self.assertEqual(
            movement.balance_after,
            Decimal("2.000"),
        )

    def test_mixed_receipt_creates_two_movement_records(self):
        receipt = self.receive()

        receipt_item = StockTransferReceiptItem.objects.get(
            receipt=receipt,
        )

        movements = StockMovement.objects.filter(
            transfer_receipt_item=receipt_item,
        )

        self.assertEqual(movements.count(), 2)

    def test_good_only_receipt_does_not_create_damaged_movement(self):
        receipt = self.receive(
            items=[
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "good_quantity": "5.000",
                    "damaged_quantity": "0",
                },
            ],
        )

        receipt_item = StockTransferReceiptItem.objects.get(
            receipt=receipt,
        )

        self.assertEqual(
            StockMovement.objects.filter(
                transfer_receipt_item=receipt_item,
            ).count(),
            1,
        )

        balance = self.get_branch_balance()

        self.assertEqual(balance.quantity, Decimal("5.000"))
        self.assertEqual(
            balance.damaged_quantity,
            Decimal("0.000"),
        )

    def test_damaged_only_receipt_does_not_create_good_movement(self):
        receipt = self.receive(
            items=[
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "good_quantity": "0",
                    "damaged_quantity": "5.000",
                },
            ],
        )

        receipt_item = StockTransferReceiptItem.objects.get(
            receipt=receipt,
        )

        self.assertEqual(
            StockMovement.objects.filter(
                transfer_receipt_item=receipt_item,
            ).count(),
            1,
        )

        balance = self.get_branch_balance()

        self.assertEqual(balance.quantity, Decimal("0.000"))
        self.assertEqual(
            balance.damaged_quantity,
            Decimal("5.000"),
        )

    # --------------------------------------------------
    # TRANSFER STATUS
    # --------------------------------------------------

    def test_partial_receipt_updates_transfer_status(self):
        self.receive(
            items=[
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "good_quantity": "2.000",
                    "damaged_quantity": "0",
                },
            ],
        )

        self.transfer.refresh_from_db()

        self.assertEqual(
            self.transfer.status,
            StockTransfer.Status.PARTIALLY_RECEIVED,
        )

    def test_receiving_all_dispatched_quantity_updates_status(self):
        self.receive()

        self.transfer.refresh_from_db()

        self.assertEqual(
            self.transfer.status,
            StockTransfer.Status.PARTIALLY_RECEIVED,
        )

    def test_receiving_entire_order_updates_status_to_received(self):
        self.dispatch(
            quantity="5.000",
            reference="RECEIPT-DISPATCH-002",
        )

        self.receive(
            reference="RECEIPT-002",
            items=[
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "good_quantity": "10.000",
                    "damaged_quantity": "0",
                },
            ],
        )

        self.transfer.refresh_from_db()

        self.assertEqual(
            self.transfer.status,
            StockTransfer.Status.RECEIVED,
        )

    # --------------------------------------------------
    # MULTIPLE RECEIPT BATCHES
    # --------------------------------------------------

    def test_multiple_receipt_batches_are_supported(self):
        self.receive(
            reference="RECEIPT-BATCH-001",
            items=[
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "good_quantity": "2.000",
                    "damaged_quantity": "0",
                },
            ],
        )

        self.receive(
            reference="RECEIPT-BATCH-002",
            items=[
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "good_quantity": "1.000",
                    "damaged_quantity": "2.000",
                },
            ],
        )

        self.assertEqual(
            StockTransferReceipt.objects.filter(
                transfer=self.transfer
            ).count(),
            2,
        )

        balance = self.get_branch_balance()

        self.assertEqual(balance.quantity, Decimal("3.000"))
        self.assertEqual(
            balance.damaged_quantity,
            Decimal("2.000"),
        )

    def test_cumulative_receipt_cannot_exceed_dispatched_quantity(self):
        self.receive(
            items=[
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "good_quantity": "4.000",
                    "damaged_quantity": "0",
                },
            ],
        )

        with self.assertRaises(ValidationError):
            self.receive(
                reference="RECEIPT-OVER",
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "good_quantity": "2.000",
                        "damaged_quantity": "0",
                    },
                ],
            )

        self.assertEqual(
            StockTransferReceipt.objects.filter(
                transfer=self.transfer
            ).count(),
            1,
        )

        self.assertEqual(
            self.get_branch_balance().quantity,
            Decimal("4.000"),
        )

    def test_receipt_can_follow_a_later_dispatch_batch(self):
        self.receive(
            items=[
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "good_quantity": "5.000",
                    "damaged_quantity": "0",
                },
            ],
        )

        self.dispatch(
            quantity="5.000",
            reference="RECEIPT-DISPATCH-002",
        )

        self.receive(
            reference="RECEIPT-BATCH-002",
            items=[
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "good_quantity": "5.000",
                    "damaged_quantity": "0",
                },
            ],
        )

        self.transfer.refresh_from_db()

        self.assertEqual(
            self.transfer.status,
            StockTransfer.Status.RECEIVED,
        )

        self.assertEqual(
            self.get_branch_balance().quantity,
            Decimal("10.000"),
        )

    # --------------------------------------------------
    # ACCESS VALIDATION
    # --------------------------------------------------

    def test_company_owner_cannot_receive_into_read_only_branch(self):
        with self.assertRaises(PermissionDenied):
            self.receive(
                user=self.company_owner,
            )

        self.assertEqual(
            StockTransferReceipt.objects.filter(
                transfer=self.transfer
            ).count(),
            0,
        )

    # --------------------------------------------------
    # QUANTITY VALIDATION
    # --------------------------------------------------

    def test_both_zero_quantities_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive(
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "good_quantity": "0",
                        "damaged_quantity": "0",
                    },
                ],
            )

        self.assertEqual(StockTransferReceipt.objects.count(), 0)

    def test_negative_good_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive(
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "good_quantity": "-1",
                        "damaged_quantity": "1",
                    },
                ],
            )

        self.assertEqual(StockTransferReceipt.objects.count(), 0)

    def test_negative_damaged_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive(
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "good_quantity": "1",
                        "damaged_quantity": "-1",
                    },
                ],
            )

        self.assertEqual(StockTransferReceipt.objects.count(), 0)

    def test_more_than_three_decimal_places_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive(
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "good_quantity": "1.0001",
                        "damaged_quantity": "0",
                    },
                ],
            )

        self.assertEqual(StockTransferReceipt.objects.count(), 0)

    def test_float_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive(
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "good_quantity": 1.5,
                        "damaged_quantity": "0",
                    },
                ],
            )

        self.assertEqual(StockTransferReceipt.objects.count(), 0)

    def test_integer_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive(
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "good_quantity": 1,
                        "damaged_quantity": "0",
                    },
                ],
            )

        self.assertEqual(StockTransferReceipt.objects.count(), 0)

    def test_boolean_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive(
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "good_quantity": True,
                        "damaged_quantity": "0",
                    },
                ],
            )

        self.assertEqual(StockTransferReceipt.objects.count(), 0)

    def test_quantity_over_field_limit_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive(
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "good_quantity": "100000000000.000",
                        "damaged_quantity": "0",
                    },
                ],
            )

        self.assertEqual(StockTransferReceipt.objects.count(), 0)

    # --------------------------------------------------
    # TRANSFER VALIDATION
    # --------------------------------------------------

    def test_receipt_before_any_dispatch_is_rejected(self):
        transfer = create_stock_transfer_order(
            user=self.branch_admin,
            branch=self.branch,
            items=[
                {
                    "product_id": self.second_product.pk,
                    "quantity": "5.000",
                },
            ],
            reference="RECEIPT-NO-DISPATCH-ORDER",
        )

        transfer_item = transfer.items.get(
            product=self.second_product
        )

        with self.assertRaises(ValidationError):
            receive_stock_transfer(
                user=self.branch_admin,
                transfer_id=transfer.pk,
                items=[
                    {
                        "transfer_item_id": transfer_item.pk,
                        "good_quantity": "1.000",
                        "damaged_quantity": "0",
                    },
                ],
                reference="RECEIPT-NO-DISPATCH",
            )

        self.assertEqual(StockTransferReceipt.objects.count(), 0)

    def test_cancelled_transfer_cannot_be_received(self):
        self.transfer.status = StockTransfer.Status.CANCELLED
        self.transfer.save(update_fields=["status"])

        with self.assertRaises(ValidationError):
            self.receive()

        self.assertEqual(StockTransferReceipt.objects.count(), 0)

    def test_duplicate_transfer_item_in_receipt_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.receive(
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "good_quantity": "1.000",
                        "damaged_quantity": "0",
                    },
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "good_quantity": "1.000",
                        "damaged_quantity": "0",
                    },
                ],
            )

        self.assertEqual(StockTransferReceipt.objects.count(), 0)

    def test_item_from_another_transfer_is_rejected(self):
        another_transfer = create_stock_transfer_order(
            user=self.branch_admin,
            branch=self.branch,
            items=[
                {
                    "product_id": self.second_product.pk,
                    "quantity": "5.000",
                },
            ],
            reference="RECEIPT-OTHER-ORDER",
        )

        another_item = another_transfer.items.get(
            product=self.second_product
        )

        with self.assertRaises(ValidationError):
            self.receive(
                items=[
                    {
                        "transfer_item_id": another_item.pk,
                        "good_quantity": "1.000",
                        "damaged_quantity": "0",
                    },
                ],
            )

        self.assertEqual(StockTransferReceipt.objects.count(), 0)

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
            reference="RECEIPT-ROLLBACK-ORDER",
        )

        transfer_items = {
            item.product_id: item.pk
            for item in transfer.items.all()
        }

        # Dispatch only the first product.
        self.dispatch(
            quantity="5.000",
            reference="RECEIPT-ROLLBACK-DISPATCH",
            transfer=transfer,
            transfer_item=transfer.items.get(
                product=self.product
            ),
        )

        with self.assertRaises(ValidationError):
            receive_stock_transfer(
                user=self.branch_admin,
                transfer_id=transfer.pk,
                items=[
                    {
                        "transfer_item_id": transfer_items[
                            self.product.pk
                        ],
                        "good_quantity": "2.000",
                        "damaged_quantity": "0",
                    },
                    {
                        "transfer_item_id": transfer_items[
                            self.second_product.pk
                        ],
                        "good_quantity": "1.000",
                        "damaged_quantity": "0",
                    },
                ],
                reference="RECEIPT-ROLLBACK",
            )

        self.assertEqual(
            StockTransferReceipt.objects.filter(
                transfer=transfer
            ).count(),
            0,
        )

        self.assertEqual(
            StockMovement.objects.filter(
                movement_type=StockMovement.MovementType.TRANSFER_IN
            ).count(),
            0,
        )

        self.assertFalse(
            StockBalance.objects.filter(
                product=self.product,
                business=self.branch,
            ).exists()
        )

    # --------------------------------------------------
    # IMMUTABLE HISTORY
    # --------------------------------------------------

    def test_receipt_event_cannot_be_updated_or_deleted(self):
        receipt = self.receive()

        with self.assertRaises((ValidationError, Exception)):
            receipt.reference = "CHANGED-REFERENCE"
            receipt.save()

        with self.assertRaises((ValidationError, Exception)):
            receipt.delete()
