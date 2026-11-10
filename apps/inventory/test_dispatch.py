
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
    StockTransferDispatchItem,
)
from apps.inventory.services.transfers.orders import (
    create_stock_transfer_order,
)
from apps.inventory.services.transfers.dispatch import (
    dispatch_stock_transfer,
)


class StockTransferDispatchServiceTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Dispatch Test Company",
            code="DISPATCH-CO",
        )

        cls.head_office = Business.objects.create(
            name="Dispatch Test HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=cls.company,
            business_code="DISPATCH-HO",
        )

        cls.branch = Business.objects.create(
            name="Dispatch Test Branch",
            business_type=Business.BusinessType.BRANCH,
            company=cls.company,
            parent_business=cls.head_office,
            business_code="DISPATCH-BR",
        )

        cls.product = Product.objects.create(
            owner_business=cls.head_office,
            stock_no="DISPATCH-001",
            name="Dispatch Product",
            purchase_price=Decimal("100.00"),
            sale_price=Decimal("150.00"),
        )

        cls.second_product = Product.objects.create(
            owner_business=cls.head_office,
            stock_no="DISPATCH-002",
            name="Second Dispatch Product",
            purchase_price=Decimal("200.00"),
            sale_price=Decimal("250.00"),
        )

        cls.branch_admin = User.objects.create_user(
            username="dispatch-branch-admin",
            password="TestPass123!",
            role=User.Role.OWNER,
            business=cls.branch,
        )

        cls.company_owner = User.objects.create_user(
            username="dispatch-company-owner",
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
            reference="DISPATCH-ORDER-001",
            notes="Dispatch test order",
        )

        self.balance = StockBalance.objects.create(
            product=self.product,
            business=self.head_office,
            quantity=Decimal("20.000"),
            damaged_quantity=Decimal("3.000"),
        )

    def dispatch(self, **overrides):
        params = {
            "user": self.company_owner,
            "transfer_id": self.transfer.pk,
            "items": [
                {
                    "transfer_item_id": self.transfer.items.get(
                        product=self.product
                    ).pk,
                    "quantity": "5.000",
                },
            ],
            "reference": "DISPATCH-001",
            "notes": "Test dispatch",
        }

        params.update(overrides)

        return dispatch_stock_transfer(**params)

    def get_balance(self, product=None):
        return StockBalance.objects.get(
            product=product or self.product,
            business=self.head_office,
        )

    # --------------------------------------------------
    # SUCCESSFUL DISPATCH
    # --------------------------------------------------

    def test_head_office_can_dispatch_stock_to_branch(self):
        dispatch = self.dispatch()

        self.assertIsNotNone(dispatch.pk)
        self.assertEqual(
            dispatch.transfer,
            self.transfer,
        )
        self.assertEqual(
            dispatch.dispatched_by,
            self.company_owner,
        )

    def test_dispatch_creates_dispatch_item(self):
        dispatch = self.dispatch()

        dispatch_item = StockTransferDispatchItem.objects.get(
            dispatch=dispatch
        )

        self.assertEqual(
            dispatch_item.transfer_item.product,
            self.product,
        )
        self.assertEqual(
            dispatch_item.quantity,
            Decimal("5.000"),
        )

    def test_dispatch_reduces_head_office_good_stock(self):
        self.dispatch()

        balance = self.get_balance()

        self.assertEqual(
            balance.quantity,
            Decimal("15.000"),
        )

    def test_dispatch_does_not_change_damaged_stock(self):
        self.dispatch()

        balance = self.get_balance()

        self.assertEqual(
            balance.damaged_quantity,
            Decimal("3.000"),
        )

    def test_dispatch_creates_stock_movement(self):
        self.dispatch()

        movement = StockMovement.objects.get(
            stock_balance=self.balance,
        )

        self.assertEqual(
            movement.movement_type,
            StockMovement.MovementType.TRANSFER_OUT,
        )
        self.assertEqual(
            movement.stock_condition,
            StockMovement.StockCondition.GOOD,
        )
        self.assertEqual(
            movement.quantity,
            Decimal("5.000"),
        )
        self.assertEqual(
            movement.balance_before,
            Decimal("20.000"),
        )
        self.assertEqual(
            movement.balance_after,
            Decimal("15.000"),
        )
        self.assertEqual(
            movement.created_by,
            self.company_owner,
        )

    def test_dispatch_movement_references_dispatch_item(self):
        dispatch = self.dispatch()

        dispatch_item = StockTransferDispatchItem.objects.get(
            dispatch=dispatch
        )

        movement = StockMovement.objects.get(
            transfer_dispatch_item=dispatch_item,
        )

        self.assertEqual(
            movement.stock_balance,
            self.balance,
        )

    # --------------------------------------------------
    # TRANSFER STATUS
    # --------------------------------------------------

    def test_partial_dispatch_updates_transfer_status(self):
        self.dispatch()

        self.transfer.refresh_from_db()

        self.assertEqual(
            self.transfer.status,
            StockTransfer.Status.PARTIALLY_DISPATCHED,
        )

    def test_full_dispatch_updates_transfer_status(self):
        self.dispatch(
            items=[
                {
                    "transfer_item_id": self.transfer.items.get(
                        product=self.product
                    ).pk,
                    "quantity": "10.000",
                },
            ],
        )

        self.transfer.refresh_from_db()

        self.assertEqual(
            self.transfer.status,
            StockTransfer.Status.DISPATCHED,
        )

    def test_multiple_dispatch_batches_are_supported(self):
        transfer_item_id = self.transfer.items.get(
            product=self.product
        ).pk

        self.dispatch(
            reference="DISPATCH-BATCH-001",
            items=[
                {
                    "transfer_item_id": transfer_item_id,
                    "quantity": "4.000",
                },
            ],
        )

        self.transfer.refresh_from_db()

        self.assertEqual(
            self.transfer.status,
            StockTransfer.Status.PARTIALLY_DISPATCHED,
        )

        self.dispatch(
            reference="DISPATCH-BATCH-002",
            items=[
                {
                    "transfer_item_id": transfer_item_id,
                    "quantity": "6.000",
                },
            ],
        )

        self.transfer.refresh_from_db()

        self.assertEqual(
            self.transfer.status,
            StockTransfer.Status.DISPATCHED,
        )

        self.assertEqual(
            StockTransferDispatch.objects.filter(
                transfer=self.transfer
            ).count(),
            2,
        )

        self.assertEqual(
            self.get_balance().quantity,
            Decimal("10.000"),
        )

    # --------------------------------------------------
    # ACCESS VALIDATION
    # --------------------------------------------------

    def test_branch_admin_cannot_dispatch_from_head_office(self):
        with self.assertRaises(PermissionDenied):
            self.dispatch(
                user=self.branch_admin,
            )

        self.assertEqual(
            StockTransferDispatch.objects.count(),
            0,
        )

        self.assertEqual(
            self.get_balance().quantity,
            Decimal("20.000"),
        )

    # --------------------------------------------------
    # QUANTITY VALIDATION
    # --------------------------------------------------

    def test_zero_dispatch_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.dispatch(
                items=[
                    {
                        "transfer_item_id": self.transfer.items.get(
                            product=self.product
                        ).pk,
                        "quantity": "0",
                    },
                ],
            )

        self.assertEqual(
            StockTransferDispatch.objects.count(),
            0,
        )

    def test_negative_dispatch_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.dispatch(
                items=[
                    {
                        "transfer_item_id": self.transfer.items.get(
                            product=self.product
                        ).pk,
                        "quantity": "-2",
                    },
                ],
            )

        self.assertEqual(
            StockTransferDispatch.objects.count(),
            0,
        )

    def test_duplicate_transfer_item_is_rejected(self):
        transfer_item_id = self.transfer.items.get(
            product=self.product
        ).pk

        with self.assertRaises(ValidationError):
            self.dispatch(
                items=[
                    {
                        "transfer_item_id": transfer_item_id,
                        "quantity": "2.000",
                    },
                    {
                        "transfer_item_id": transfer_item_id,
                        "quantity": "3.000",
                    },
                ],
            )

        self.assertEqual(
            StockTransferDispatch.objects.count(),
            0,
        )

    def test_over_dispatch_is_rejected(self):
        transfer_item_id = self.transfer.items.get(
            product=self.product
        ).pk

        self.dispatch(
            items=[
                {
                    "transfer_item_id": transfer_item_id,
                    "quantity": "8.000",
                },
            ],
        )

        with self.assertRaises(ValidationError):
            self.dispatch(
                reference="DISPATCH-OVER",
                items=[
                    {
                        "transfer_item_id": transfer_item_id,
                        "quantity": "3.000",
                    },
                ],
            )

        self.assertEqual(
            StockTransferDispatch.objects.count(),
            1,
        )

        self.assertEqual(
            self.get_balance().quantity,
            Decimal("12.000"),
        )

    def test_dispatch_more_than_available_stock_is_rejected(self):
        self.balance.quantity = Decimal("4.000")
        self.balance.save(update_fields=["quantity"])

        with self.assertRaises(ValidationError):
            self.dispatch(
                items=[
                    {
                        "transfer_item_id": self.transfer.items.get(
                            product=self.product
                        ).pk,
                        "quantity": "5.000",
                    },
                ],
            )

        self.assertEqual(
            StockTransferDispatch.objects.count(),
            0,
        )

        self.assertEqual(
            self.get_balance().quantity,
            Decimal("4.000"),
        )

    # --------------------------------------------------
    # PRODUCT VALIDATION
    # --------------------------------------------------

    def test_inactive_product_cannot_be_dispatched(self):
        self.product.is_active = False
        self.product.save(update_fields=["is_active"])

        with self.assertRaises(ValidationError):
            self.dispatch()

        self.assertEqual(
            StockTransferDispatch.objects.count(),
            0,
        )

    # --------------------------------------------------
    # TRANSACTION ROLLBACK
    # --------------------------------------------------

    def test_insufficient_stock_on_later_item_rolls_back_everything(self):
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
            reference="DISPATCH-ROLLBACK-ORDER",
        )

        StockBalance.objects.create(
            product=self.second_product,
            business=self.head_office,
            quantity=Decimal("2.000"),
            damaged_quantity=Decimal("0.000"),
        )

        transfer_items = {
            item.product_id: item.pk
            for item in transfer.items.all()
        }

        with self.assertRaises(ValidationError):
            dispatch_stock_transfer(
                user=self.company_owner,
                transfer_id=transfer.pk,
                items=[
                    {
                        "transfer_item_id": transfer_items[
                            self.product.pk
                        ],
                        "quantity": "5.000",
                    },
                    {
                        "transfer_item_id": transfer_items[
                            self.second_product.pk
                        ],
                        "quantity": "5.000",
                    },
                ],
                reference="DISPATCH-ROLLBACK",
            )

        self.assertEqual(
            StockTransferDispatch.objects.filter(
                transfer=transfer
            ).count(),
            0,
        )

        self.assertEqual(
            StockMovement.objects.filter(
                movement_type=StockMovement.MovementType.TRANSFER_OUT
            ).count(),
            0,
        )

        self.assertEqual(
            self.get_balance().quantity,
            Decimal("20.000"),
        )

        second_balance = self.get_balance(
            product=self.second_product
        )

        self.assertEqual(
            second_balance.quantity,
            Decimal("2.000"),
        )

    # --------------------------------------------------
    # CANCELLED TRANSFER
    # --------------------------------------------------

    def test_cancelled_transfer_cannot_be_dispatched(self):
        self.transfer.status = StockTransfer.Status.CANCELLED
        self.transfer.save(update_fields=["status"])

        with self.assertRaises(ValidationError):
            self.dispatch()

        self.assertEqual(
            StockTransferDispatch.objects.count(),
            0,
        )
