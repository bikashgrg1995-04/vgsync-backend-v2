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
    StockTransferReturn,
    StockTransferReturnItem,
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


class StockTransferReturnServiceTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Return Test Company",
            code="RETURN-CO",
        )

        cls.head_office = Business.objects.create(
            name="Return Test HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=cls.company,
            business_code="RETURN-HO",
        )

        cls.branch = Business.objects.create(
            name="Return Test Branch",
            business_type=Business.BusinessType.BRANCH,
            company=cls.company,
            parent_business=cls.head_office,
            business_code="RETURN-BR",
        )

        cls.product = Product.objects.create(
            owner_business=cls.head_office,
            stock_no="RETURN-001",
            name="Return Product",
            purchase_price=Decimal("100.00"),
            sale_price=Decimal("150.00"),
        )

        cls.second_product = Product.objects.create(
            owner_business=cls.head_office,
            stock_no="RETURN-002",
            name="Second Return Product",
            purchase_price=Decimal("200.00"),
            sale_price=Decimal("250.00"),
        )

        cls.branch_admin = User.objects.create_user(
            username="return-branch-admin",
            password="TestPass123!",
            role=User.Role.OWNER,
            business=cls.branch,
        )

        cls.company_owner = User.objects.create_user(
            username="return-company-owner",
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
            reference="RETURN-ORDER-001",
            notes="Return test order",
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

        self.dispatch(
            quantity="5.000",
            reference="RETURN-DISPATCH-001",
        )

        self.receive(
            good_quantity="3.000",
            damaged_quantity="2.000",
            reference="RETURN-RECEIPT-001",
        )

    # --------------------------------------------------
    # HELPERS
    # --------------------------------------------------

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

    def receive(
        self,
        *,
        good_quantity,
        damaged_quantity,
        reference,
        transfer=None,
        transfer_item=None,
    ):
        transfer = transfer or self.transfer
        transfer_item = transfer_item or transfer.items.get(
            product=self.product
        )

        return receive_stock_transfer(
            user=self.branch_admin,
            transfer_id=transfer.pk,
            items=[
                {
                    "transfer_item_id": transfer_item.pk,
                    "good_quantity": good_quantity,
                    "damaged_quantity": damaged_quantity,
                },
            ],
            reference=reference,
        )

    def return_stock(self, **overrides):
        params = {
            "user": self.branch_admin,
            "transfer_id": self.transfer.pk,
            "items": [
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "quantity": "1.000",
                },
            ],
            "reference": "RETURN-BATCH-001",
            "notes": "Damaged goods returned",
        }

        params.update(overrides)

        return return_damaged_stock_transfer(**params)

    def get_branch_balance(self, product=None):
        return StockBalance.objects.get(
            product=product or self.product,
            business=self.branch,
        )

    # --------------------------------------------------
    # SUCCESSFUL RETURN
    # --------------------------------------------------

    def test_branch_can_return_received_damaged_stock(self):
        return_batch = self.return_stock()

        self.assertIsNotNone(return_batch.pk)
        self.assertEqual(return_batch.transfer, self.transfer)
        self.assertEqual(
            return_batch.dispatched_by,
            self.branch_admin,
        )

    def test_return_creates_return_item(self):
        return_batch = self.return_stock()

        return_item = StockTransferReturnItem.objects.get(
            return_batch=return_batch,
        )

        self.assertEqual(
            return_item.transfer_item,
            self.transfer_item,
        )
        self.assertEqual(
            return_item.quantity,
            Decimal("1.000"),
        )

    def test_return_reduces_branch_damaged_stock(self):
        self.return_stock()

        balance = self.get_branch_balance()

        self.assertEqual(
            balance.damaged_quantity,
            Decimal("1.000"),
        )

    def test_return_does_not_change_branch_good_stock(self):
        self.return_stock()

        balance = self.get_branch_balance()

        self.assertEqual(
            balance.quantity,
            Decimal("3.000"),
        )

    def test_return_does_not_change_head_office_stock(self):
        self.return_stock()

        self.head_office_balance.refresh_from_db()

        self.assertEqual(
            self.head_office_balance.quantity,
            Decimal("15.000"),
        )
        self.assertEqual(
            self.head_office_balance.damaged_quantity,
            Decimal("3.000"),
        )

    def test_return_creates_damaged_transfer_out_movement(self):
        return_batch = self.return_stock()

        return_item = StockTransferReturnItem.objects.get(
            return_batch=return_batch,
        )

        movement = StockMovement.objects.get(
            transfer_return_item=return_item,
        )

        self.assertEqual(
            movement.stock_condition,
            StockMovement.StockCondition.DAMAGED,
        )
        self.assertEqual(
            movement.movement_type,
            StockMovement.MovementType.TRANSFER_OUT,
        )
        self.assertEqual(
            movement.quantity,
            Decimal("1.000"),
        )
        self.assertEqual(
            movement.balance_before,
            Decimal("2.000"),
        )
        self.assertEqual(
            movement.balance_after,
            Decimal("1.000"),
        )
        self.assertEqual(
            movement.created_by,
            self.branch_admin,
        )

    def test_return_movement_references_correct_event_item(self):
        return_batch = self.return_stock()

        return_item = StockTransferReturnItem.objects.get(
            return_batch=return_batch,
        )

        movement = StockMovement.objects.get(
            transfer_return_item=return_item,
        )

        self.assertEqual(
            movement.stock_balance.business,
            self.branch,
        )
        self.assertEqual(
            movement.stock_balance.product,
            self.product,
        )
        self.assertEqual(
            movement.reference,
            return_batch.reference,
        )

    # --------------------------------------------------
    # PARTIAL AND MULTIPLE RETURNS
    # --------------------------------------------------

    def test_partial_damaged_return_is_supported(self):
        self.return_stock(
            items=[
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "quantity": "0.500",
                },
            ],
        )

        self.assertEqual(
            self.get_branch_balance().damaged_quantity,
            Decimal("1.500"),
        )

    def test_multiple_return_batches_are_supported(self):
        self.return_stock(
            reference="RETURN-BATCH-001",
            items=[
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "quantity": "0.500",
                },
            ],
        )

        self.return_stock(
            reference="RETURN-BATCH-002",
            items=[
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "quantity": "1.000",
                },
            ],
        )

        self.assertEqual(
            StockTransferReturn.objects.filter(
                transfer=self.transfer
            ).count(),
            2,
        )

        self.assertEqual(
            self.get_branch_balance().damaged_quantity,
            Decimal("0.500"),
        )

    def test_entire_damaged_quantity_can_be_returned(self):
        self.return_stock(
            items=[
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "quantity": "2.000",
                },
            ],
        )

        self.assertEqual(
            self.get_branch_balance().damaged_quantity,
            Decimal("0.000"),
        )

    def test_cumulative_return_cannot_exceed_damaged_received(self):
        self.return_stock(
            items=[
                {
                    "transfer_item_id": self.transfer_item.pk,
                    "quantity": "1.500",
                },
            ],
        )

        with self.assertRaises(ValidationError):
            self.return_stock(
                reference="RETURN-OVER",
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "quantity": "0.501",
                    },
                ],
            )

        self.assertEqual(
            StockTransferReturn.objects.filter(
                transfer=self.transfer
            ).count(),
            1,
        )

        self.assertEqual(
            self.get_branch_balance().damaged_quantity,
            Decimal("0.500"),
        )

    def test_good_received_quantity_cannot_be_returned_as_damaged(self):
        with self.assertRaises(ValidationError):
            self.return_stock(
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "quantity": "3.000",
                    },
                ],
            )

        self.assertEqual(StockTransferReturn.objects.count(), 0)

    # --------------------------------------------------
    # STOCK VALIDATION
    # --------------------------------------------------

    def test_insufficient_branch_damaged_stock_is_rejected(self):
        balance = self.get_branch_balance()
        balance.damaged_quantity = Decimal("0.500")
        balance.save(update_fields=["damaged_quantity"])

        with self.assertRaises(ValidationError):
            self.return_stock(
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "quantity": "1.000",
                    },
                ],
            )

        self.assertEqual(StockTransferReturn.objects.count(), 0)

        balance.refresh_from_db()

        self.assertEqual(
            balance.damaged_quantity,
            Decimal("0.500"),
        )

    # --------------------------------------------------
    # ACCESS VALIDATION
    # --------------------------------------------------

    def test_company_owner_cannot_return_from_read_only_branch(self):
        with self.assertRaises(PermissionDenied):
            self.return_stock(
                user=self.company_owner,
            )

        self.assertEqual(StockTransferReturn.objects.count(), 0)

        self.assertEqual(
            self.get_branch_balance().damaged_quantity,
            Decimal("2.000"),
        )

    # --------------------------------------------------
    # QUANTITY VALIDATION
    # --------------------------------------------------

    def test_zero_return_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.return_stock(
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "quantity": "0",
                    },
                ],
            )

        self.assertEqual(StockTransferReturn.objects.count(), 0)

    def test_negative_return_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.return_stock(
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "quantity": "-1.000",
                    },
                ],
            )

        self.assertEqual(StockTransferReturn.objects.count(), 0)

    def test_float_return_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.return_stock(
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "quantity": 1.5,
                    },
                ],
            )

        self.assertEqual(StockTransferReturn.objects.count(), 0)

    def test_integer_return_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.return_stock(
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "quantity": 1,
                    },
                ],
            )

        self.assertEqual(StockTransferReturn.objects.count(), 0)

    def test_boolean_return_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.return_stock(
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "quantity": True,
                    },
                ],
            )

        self.assertEqual(StockTransferReturn.objects.count(), 0)

    def test_more_than_three_decimal_places_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.return_stock(
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "quantity": "0.1234",
                    },
                ],
            )

        self.assertEqual(StockTransferReturn.objects.count(), 0)

    def test_quantity_over_field_limit_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.return_stock(
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "quantity": "100000000000.000",
                    },
                ],
            )

        self.assertEqual(StockTransferReturn.objects.count(), 0)

    # --------------------------------------------------
    # TRANSFER VALIDATION
    # --------------------------------------------------

    def test_cancelled_transfer_cannot_be_returned(self):
        self.transfer.status = StockTransfer.Status.CANCELLED
        self.transfer.save(update_fields=["status"])

        with self.assertRaises(ValidationError):
            self.return_stock()

        self.assertEqual(StockTransferReturn.objects.count(), 0)

    def test_return_before_receipt_is_rejected(self):
        transfer = create_stock_transfer_order(
            user=self.branch_admin,
            branch=self.branch,
            items=[
                {
                    "product_id": self.second_product.pk,
                    "quantity": "5.000",
                },
            ],
            reference="RETURN-NO-RECEIPT-ORDER",
        )

        transfer_item = transfer.items.get(
            product=self.second_product
        )

        with self.assertRaises(ValidationError):
            return_damaged_stock_transfer(
                user=self.branch_admin,
                transfer_id=transfer.pk,
                items=[
                    {
                        "transfer_item_id": transfer_item.pk,
                        "quantity": "1.000",
                    },
                ],
                reference="RETURN-NO-RECEIPT",
            )

        self.assertEqual(StockTransferReturn.objects.count(), 0)

    def test_duplicate_transfer_item_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.return_stock(
                items=[
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "quantity": "0.500",
                    },
                    {
                        "transfer_item_id": self.transfer_item.pk,
                        "quantity": "0.500",
                    },
                ],
            )

        self.assertEqual(StockTransferReturn.objects.count(), 0)

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
            reference="RETURN-OTHER-ORDER",
        )

        another_item = another_transfer.items.get(
            product=self.second_product
        )

        with self.assertRaises(ValidationError):
            self.return_stock(
                items=[
                    {
                        "transfer_item_id": another_item.pk,
                        "quantity": "1.000",
                    },
                ],
            )

        self.assertEqual(StockTransferReturn.objects.count(), 0)

    def test_empty_return_items_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.return_stock(items=[])

        self.assertEqual(StockTransferReturn.objects.count(), 0)

    # --------------------------------------------------
    # TRANSACTION ROLLBACK
    # --------------------------------------------------

    def test_invalid_later_item_rolls_back_return(self):
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
            reference="RETURN-ROLLBACK-ORDER",
        )

        transfer_items = {
            item.product_id: item
            for item in transfer.items.all()
        }

        self.dispatch(
            quantity="5.000",
            reference="RETURN-ROLLBACK-DISPATCH",
            transfer=transfer,
            transfer_item=transfer_items[self.product.pk],
        )

        self.receive(
            good_quantity="0",
            damaged_quantity="2.000",
            reference="RETURN-ROLLBACK-RECEIPT",
            transfer=transfer,
            transfer_item=transfer_items[self.product.pk],
        )

        branch_balance = self.get_branch_balance()

        with self.assertRaises(ValidationError):
            return_damaged_stock_transfer(
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
                reference="RETURN-ROLLBACK",
            )

        self.assertEqual(
            StockTransferReturn.objects.filter(
                transfer=transfer
            ).count(),
            0,
        )

        self.assertEqual(
            StockMovement.objects.filter(
                transfer_return_item__isnull=False
            ).count(),
            0,
        )

        branch_balance.refresh_from_db()

        self.assertEqual(
            branch_balance.damaged_quantity,
            Decimal("4.000"),
        )

    # --------------------------------------------------
    # IMMUTABLE HISTORY
    # --------------------------------------------------

    def test_return_event_cannot_be_updated_or_deleted(self):
        return_batch = self.return_stock()

        with self.assertRaises(ValidationError):
            return_batch.reference = "CHANGED-RETURN"
            return_batch.save()

        with self.assertRaises(ValidationError):
            return_batch.delete()

    def test_return_item_cannot_be_updated_or_deleted(self):
        return_batch = self.return_stock()

        return_item = StockTransferReturnItem.objects.get(
            return_batch=return_batch,
        )

        with self.assertRaises(ValidationError):
            return_item.quantity = Decimal("0.500")
            return_item.save()

        with self.assertRaises(ValidationError):
            return_item.delete()