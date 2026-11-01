
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
    StockTransferReturnReview,
    StockTransferReturnReviewItem,
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
from apps.inventory.services.transfers.review import (
    review_stock_transfer_return,
)


class StockTransferReturnReviewServiceTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Return Review Test Company",
            code="RETURN-REVIEW-CO",
        )

        cls.head_office = Business.objects.create(
            name="Return Review Test HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=cls.company,
            business_code="RETURN-REVIEW-HO",
        )

        cls.branch = Business.objects.create(
            name="Return Review Test Branch",
            business_type=Business.BusinessType.BRANCH,
            company=cls.company,
            parent_business=cls.head_office,
            business_code="RETURN-REVIEW-BR",
        )

        cls.product = Product.objects.create(
            owner_business=cls.head_office,
            stock_no="RETURN-REVIEW-001",
            name="Return Review Product",
            purchase_price=Decimal("100.00"),
            sale_price=Decimal("150.00"),
        )

        cls.second_product = Product.objects.create(
            owner_business=cls.head_office,
            stock_no="RETURN-REVIEW-002",
            name="Second Return Review Product",
            purchase_price=Decimal("200.00"),
            sale_price=Decimal("250.00"),
        )

        cls.branch_admin = User.objects.create_user(
            username="return-review-branch-admin",
            password="TestPass123!",
            role=User.Role.ADMIN,
            business=cls.branch,
        )

        cls.company_owner = User.objects.create_user(
            username="return-review-company-owner",
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
            reference="RETURN-REVIEW-ORDER-001",
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
            reference="RETURN-REVIEW-DISPATCH-001",
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
            reference="RETURN-REVIEW-BRANCH-RECEIPT-001",
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
            reference="RETURN-REVIEW-RETURN-001",
        )

        self.return_receipt = receive_stock_transfer_return(
            user=self.company_owner,
            return_batch_id=self.return_batch.pk,
            items=[
                {
                    "return_item_id": self.return_batch.items.get().pk,
                    "received_quantity": "1.000",
                },
            ],
            reference="RETURN-REVIEW-HO-RECEIPT-001",
            notes="Damaged goods received at HO.",
        )

        self.receipt_item = self.return_receipt.items.get()

    # --------------------------------------------------
    # HELPERS
    # --------------------------------------------------

    def review_return(self, **overrides):
        params = {
            "user": self.company_owner,
            "receipt_id": self.return_receipt.pk,
            "decision": "accepted",
            "remarks": "Goods inspected at Head Office.",
            "items": [
                {
                    "receipt_item_id": self.receipt_item.pk,
                    "accepted_quantity": "1.000",
                    "rejected_quantity": "0.000",
                    "remarks": "Accepted after inspection.",
                },
            ],
        }

        params.update(overrides)

        return review_stock_transfer_return(**params)

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

    def create_multi_item_receipt(self):
        """
        Create one HO return receipt containing two different products.
        Used for missing-item and transaction-rollback tests.
        """
        StockBalance.objects.create(
            product=self.second_product,
            business=self.head_office,
            quantity=Decimal("2.000"),
        )

        transfer = create_stock_transfer_order(
            user=self.branch_admin,
            branch=self.branch,
            items=[
                {
                    "product_id": self.product.pk,
                    "quantity": "2.000",
                },
                {
                    "product_id": self.second_product.pk,
                    "quantity": "2.000",
                },
            ],
            reference="RETURN-REVIEW-MULTI-ORDER",
        )

        transfer_items = {
            item.product_id: item
            for item in transfer.items.all()
        }

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
            reference="RETURN-REVIEW-MULTI-DISPATCH",
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
            reference="RETURN-REVIEW-MULTI-BRANCH-RECEIPT",
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
            reference="RETURN-REVIEW-MULTI-RETURN",
        )

        return_items = {
            item.transfer_item.product_id: item
            for item in return_batch.items.select_related(
                "transfer_item"
            )
        }

        return receive_stock_transfer_return(
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
                    "received_quantity": "1.000",
                },
            ],
            reference="RETURN-REVIEW-MULTI-HO-RECEIPT",
        )

    # --------------------------------------------------
    # SUCCESSFUL REVIEW
    # --------------------------------------------------

    def test_head_office_can_review_return_receipt(self):
        review = self.review_return()

        self.assertIsNotNone(review.pk)
        self.assertEqual(review.return_receipt, self.return_receipt)
        self.assertEqual(review.reviewed_by, self.company_owner)

    def test_review_item_is_created(self):
        review = self.review_return()

        review_item = review.items.get()

        self.assertEqual(
            review_item.receipt_item,
            self.receipt_item,
        )
        self.assertEqual(
            review_item.accepted_quantity,
            Decimal("1.000"),
        )
        self.assertEqual(
            review_item.rejected_quantity,
            Decimal("0.000"),
        )

    def test_all_accepted_decision(self):
        review = self.review_return(
            decision="accepted",
        )

        self.assertEqual(
            review.decision,
            StockTransferReturnReview.Decision.ACCEPTED,
        )

    def test_all_rejected_decision(self):
        review = self.review_return(
            decision="rejected",
            items=[
                {
                    "receipt_item_id": self.receipt_item.pk,
                    "accepted_quantity": "0.000",
                    "rejected_quantity": "1.000",
                    "remarks": "Not suitable for reuse.",
                },
            ],
        )

        self.assertEqual(
            review.decision,
            StockTransferReturnReview.Decision.REJECTED,
        )

    def test_partially_accepted_decision(self):
        review = self.review_return(
            decision="partially_accepted",
            items=[
                {
                    "receipt_item_id": self.receipt_item.pk,
                    "accepted_quantity": "0.500",
                    "rejected_quantity": "0.500",
                    "remarks": "Only half passed inspection.",
                },
            ],
        )

        self.assertEqual(
            review.decision,
            StockTransferReturnReview.Decision.PARTIALLY_ACCEPTED,
        )

    def test_review_remarks_are_saved(self):
        review = self.review_return(
            remarks="Inspection completed by HO.",
        )

        self.assertEqual(
            review.remarks,
            "Inspection completed by HO.",
        )

    def test_item_remarks_are_saved(self):
        review = self.review_return()

        review_item = review.items.get()

        self.assertEqual(
            review_item.remarks,
            "Accepted after inspection.",
        )

    # --------------------------------------------------
    # DECISION VALIDATION
    # --------------------------------------------------

    def test_wrong_decision_for_accepted_quantities_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.review_return(
                decision="rejected",
            )

        self.assertEqual(
            StockTransferReturnReview.objects.count(),
            0,
        )

    def test_wrong_decision_for_rejected_quantities_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.review_return(
                decision="accepted",
                items=[
                    {
                        "receipt_item_id": self.receipt_item.pk,
                        "accepted_quantity": "0.000",
                        "rejected_quantity": "1.000",
                    },
                ],
            )

        self.assertEqual(
            StockTransferReturnReview.objects.count(),
            0,
        )

    def test_wrong_decision_for_partial_quantities_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.review_return(
                decision="accepted",
                items=[
                    {
                        "receipt_item_id": self.receipt_item.pk,
                        "accepted_quantity": "0.500",
                        "rejected_quantity": "0.500",
                    },
                ],
            )

        self.assertEqual(
            StockTransferReturnReview.objects.count(),
            0,
        )

    def test_accepted_and_rejected_must_equal_received_quantity(self):
        with self.assertRaises(ValidationError):
            self.review_return(
                items=[
                    {
                        "receipt_item_id": self.receipt_item.pk,
                        "accepted_quantity": "0.500",
                        "rejected_quantity": "0.000",
                    },
                ],
            )

        self.assertEqual(
            StockTransferReturnReview.objects.count(),
            0,
        )

    # --------------------------------------------------
    # DUPLICATE AND ITEM VALIDATION
    # --------------------------------------------------

    def test_receipt_can_only_be_reviewed_once(self):
        self.review_return()

        with self.assertRaises(ValidationError):
            self.review_return()

        self.assertEqual(
            StockTransferReturnReview.objects.filter(
                return_receipt=self.return_receipt
            ).count(),
            1,
        )

    def test_empty_items_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.review_return(items=[])

    def test_missing_receipt_item_is_rejected(self):
        receipt = self.create_multi_item_receipt()

        receipt_items = list(receipt.items.all())

        with self.assertRaises(ValidationError):
            review_stock_transfer_return(
                user=self.company_owner,
                receipt_id=receipt.pk,
                decision="accepted",
                items=[
                    {
                        "receipt_item_id": receipt_items[0].pk,
                        "accepted_quantity": "1.000",
                        "rejected_quantity": "0.000",
                    },
                ],
            )

        self.assertFalse(
            StockTransferReturnReview.objects.filter(
                return_receipt=receipt
            ).exists()
        )

    def test_duplicate_receipt_item_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.review_return(
                items=[
                    {
                        "receipt_item_id": self.receipt_item.pk,
                        "accepted_quantity": "1.000",
                        "rejected_quantity": "0.000",
                    },
                    {
                        "receipt_item_id": self.receipt_item.pk,
                        "accepted_quantity": "1.000",
                        "rejected_quantity": "0.000",
                    },
                ],
            )

        self.assertEqual(
            StockTransferReturnReview.objects.count(),
            0,
        )

    def test_item_from_another_receipt_is_rejected(self):
        another_receipt = self.create_multi_item_receipt()
        another_item = another_receipt.items.first()

        with self.assertRaises(ValidationError):
            self.review_return(
                items=[
                    {
                        "receipt_item_id": another_item.pk,
                        "accepted_quantity": "1.000",
                        "rejected_quantity": "0.000",
                    },
                ],
            )

        self.assertEqual(
            StockTransferReturnReview.objects.count(),
            0,
        )

    def test_non_dictionary_item_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.review_return(
                items=["invalid"],
            )

    def test_invalid_receipt_item_id_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.review_return(
                items=[
                    {
                        "receipt_item_id": True,
                        "accepted_quantity": "1.000",
                        "rejected_quantity": "0.000",
                    },
                ],
            )

    # --------------------------------------------------
    # QUANTITY VALIDATION
    # --------------------------------------------------

    def test_float_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.review_return(
                items=[
                    {
                        "receipt_item_id": self.receipt_item.pk,
                        "accepted_quantity": 1.0,
                        "rejected_quantity": "0.000",
                    },
                ],
            )

    def test_integer_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.review_return(
                items=[
                    {
                        "receipt_item_id": self.receipt_item.pk,
                        "accepted_quantity": 1,
                        "rejected_quantity": "0.000",
                    },
                ],
            )

    def test_boolean_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.review_return(
                items=[
                    {
                        "receipt_item_id": self.receipt_item.pk,
                        "accepted_quantity": True,
                        "rejected_quantity": "0.000",
                    },
                ],
            )

    def test_negative_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.review_return(
                items=[
                    {
                        "receipt_item_id": self.receipt_item.pk,
                        "accepted_quantity": "-0.500",
                        "rejected_quantity": "1.500",
                    },
                ],
            )

    def test_more_than_three_decimal_places_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.review_return(
                items=[
                    {
                        "receipt_item_id": self.receipt_item.pk,
                        "accepted_quantity": "1.0001",
                        "rejected_quantity": "0.000",
                    },
                ],
            )

    def test_non_finite_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.review_return(
                items=[
                    {
                        "receipt_item_id": self.receipt_item.pk,
                        "accepted_quantity": "NaN",
                        "rejected_quantity": "0.000",
                    },
                ],
            )

    # --------------------------------------------------
    # GENERAL INPUT VALIDATION
    # --------------------------------------------------

    def test_invalid_receipt_id_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.review_return(receipt_id=True)

    def test_nonexistent_receipt_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.review_return(receipt_id=999999)

    def test_invalid_decision_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.review_return(decision="pending")

    def test_non_string_decision_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.review_return(decision=123)

    def test_non_string_remarks_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.review_return(remarks=123)

    def test_non_string_item_remarks_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.review_return(
                items=[
                    {
                        "receipt_item_id": self.receipt_item.pk,
                        "accepted_quantity": "1.000",
                        "rejected_quantity": "0.000",
                        "remarks": 123,
                    },
                ],
            )

    # --------------------------------------------------
    # ACCESS VALIDATION
    # --------------------------------------------------

    def test_branch_admin_cannot_review_at_head_office(self):
        with self.assertRaises(PermissionDenied):
            self.review_return(user=self.branch_admin)

        self.assertEqual(
            StockTransferReturnReview.objects.count(),
            0,
        )

    # --------------------------------------------------
    # STOCK MUST NOT CHANGE
    # --------------------------------------------------

    def test_review_does_not_change_head_office_good_stock(self):
        before = self.get_head_office_balance().quantity

        self.review_return()

        after = self.get_head_office_balance().quantity

        self.assertEqual(before, after)

    def test_review_does_not_change_head_office_damaged_stock(self):
        before = self.get_head_office_balance().damaged_quantity

        self.review_return()

        after = self.get_head_office_balance().damaged_quantity

        self.assertEqual(before, after)

    def test_review_does_not_change_branch_stock(self):
        before = self.get_branch_balance()

        good_before = before.quantity
        damaged_before = before.damaged_quantity

        self.review_return()

        after = self.get_branch_balance()

        self.assertEqual(after.quantity, good_before)
        self.assertEqual(after.damaged_quantity, damaged_before)

    def test_review_does_not_create_stock_movement(self):
        movement_count_before = StockMovement.objects.count()

        self.review_return()

        movement_count_after = StockMovement.objects.count()

        self.assertEqual(
            movement_count_after,
            movement_count_before,
        )

    # --------------------------------------------------
    # TRANSACTION ROLLBACK
    # --------------------------------------------------

    def test_invalid_later_item_rolls_back_review(self):
        receipt = self.create_multi_item_receipt()

        receipt_items = list(receipt.items.order_by("pk"))

        with self.assertRaises(ValidationError):
            review_stock_transfer_return(
                user=self.company_owner,
                receipt_id=receipt.pk,
                decision="accepted",
                items=[
                    {
                        "receipt_item_id": receipt_items[0].pk,
                        "accepted_quantity": "1.000",
                        "rejected_quantity": "0.000",
                    },
                    {
                        "receipt_item_id": receipt_items[1].pk,
                        "accepted_quantity": "0.500",
                        "rejected_quantity": "0.000",
                    },
                ],
            )

        self.assertFalse(
            StockTransferReturnReview.objects.filter(
                return_receipt=receipt
            ).exists()
        )

        self.assertFalse(
            StockTransferReturnReviewItem.objects.exists()
        )

    # --------------------------------------------------
    # IMMUTABLE HISTORY
    # --------------------------------------------------

    def test_review_event_cannot_be_updated_or_deleted(self):
        review = self.review_return()

        with self.assertRaises(ValidationError):
            review.remarks = "Changed review"
            review.save()

        with self.assertRaises(ValidationError):
            review.delete()

    def test_review_item_cannot_be_updated_or_deleted(self):
        review = self.review_return()
        review_item = review.items.get()

        with self.assertRaises(ValidationError):
            review_item.accepted_quantity = Decimal("0.500")
            review_item.save()

        with self.assertRaises(ValidationError):
            review_item.delete()
