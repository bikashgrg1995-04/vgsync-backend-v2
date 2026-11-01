
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


# ==================================================
# PRODUCT MASTER
# ==================================================

class Product(models.Model):
    """
    Central product master owned by a Head Office or Independent Business.
    Branches use their Head Office's product; they do not own cloned products.
    """

    owner_business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="inventory_products",
    )

    stock_no = models.CharField(max_length=100)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    brand = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=100, blank=True)
    category = models.CharField(max_length=100, blank=True)
    group = models.CharField(max_length=100, blank=True)

    unit = models.CharField(max_length=20, default="pcs")

    mrp = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    purchase_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    sale_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    image = models.FileField(
        upload_to="inventory/products/",
        blank=True,
        null=True,
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["owner_business", "stock_no"],
                name="unique_product_stock_no_per_owner",
            ),
            models.CheckConstraint(
                condition=Q(mrp__gte=0),
                name="product_mrp_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(purchase_price__gte=0),
                name="product_purchase_price_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(sale_price__gte=0),
                name="product_sale_price_non_negative",
            ),
        ]

    def clean(self):
        super().clean()

        if not self.owner_business_id:
            return

        if self.owner_business.business_type == "branch":
            raise ValidationError({
                "owner_business": (
                    "A Branch cannot own central products. "
                    "Products must be owned by its Head Office."
                )
            })

        if self.owner_business.business_type not in (
            "head_office",
            "independent",
        ):
            raise ValidationError({
                "owner_business": "Invalid product owner business type."
            })

    def __str__(self):
        return f"{self.stock_no} - {self.name}"


# ==================================================
# STOCK BALANCE
# ==================================================

class StockBalance(models.Model):
    """
    Current good and damaged stock for one product at one business.
    """

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="stock_balances",
    )

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="stock_balances",
    )

    quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=0,
    )

    damaged_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=0,
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["product__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "business"],
                name="unique_product_stock_per_business",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gte=0),
                name="stock_balance_quantity_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(damaged_quantity__gte=0),
                name="stock_balance_damaged_quantity_non_negative",
            ),
        ]

    def clean(self):
        super().clean()

        if not self.product_id or not self.business_id:
            return

        owner = self.product.owner_business
        business = self.business

        if owner.business_type == "independent":
            if business.pk != owner.pk:
                raise ValidationError({
                    "business": (
                        "Independent business products can only "
                        "have stock in their owning business."
                    )
                })

        elif owner.business_type == "head_office":
            is_head_office = business.pk == owner.pk

            is_valid_branch = (
                business.business_type == "branch"
                and business.parent_business_id == owner.pk
                and business.company_id == owner.company_id
            )

            if not is_head_office and not is_valid_branch:
                raise ValidationError({
                    "business": (
                        "Stock can only be held by the product's "
                        "Head Office or one of its own branches."
                    )
                })

        else:
            raise ValidationError({
                "product": "Invalid central product owner."
            })

    def __str__(self):
        return f"{self.product.stock_no} - {self.business.name}"


# ==================================================
# IMMUTABLE STOCK MOVEMENT QUERYSET AND MANAGER
# ==================================================

class ImmutableStockMovementQuerySet(models.QuerySet):

    def update(self, **kwargs):
        raise ValidationError("Stock movements cannot be updated.")

    def delete(self):
        raise ValidationError("Stock movements cannot be deleted.")

    def bulk_create(self, *args, **kwargs):
        raise ValidationError(
            "Create stock movements through the stock service."
        )

    def bulk_update(self, *args, **kwargs):
        raise ValidationError(
            "Stock movements cannot be bulk updated."
        )


class ImmutableStockMovementManager(
    models.Manager.from_queryset(ImmutableStockMovementQuerySet)
):
    pass


# ==================================================
# STOCK MOVEMENT
# ==================================================

class StockMovement(models.Model):
    """
    Append-only stock movement history.

    Quantity is always positive.
    Movement type defines its direction.
    Stock condition identifies which balance is affected.

    Transfer movements reference their originating event item.
    Normal stock movements leave all transfer references NULL.
    """

    class StockCondition(models.TextChoices):
        GOOD = "good", "Good Stock"
        DAMAGED = "damaged", "Damaged Stock"

    class MovementType(models.TextChoices):
        OPENING = "opening", "Opening Stock"
        PURCHASE_RECEIPT = "purchase_receipt", "Purchase Receipt"
        SALE_ISSUE = "sale_issue", "Sale Issue"
        SALE_RETURN = "sale_return", "Sale Return"
        PURCHASE_RETURN = "purchase_return", "Purchase Return"
        ADJUSTMENT_IN = "adjustment_in", "Adjustment Increase"
        ADJUSTMENT_OUT = "adjustment_out", "Adjustment Decrease"
        TRANSFER_IN = "transfer_in", "Transfer In"
        TRANSFER_OUT = "transfer_out", "Transfer Out"
        REVERSAL = "reversal", "Reversal"

    stock_balance = models.ForeignKey(
        StockBalance,
        on_delete=models.PROTECT,
        related_name="movements",
    )

    stock_condition = models.CharField(
        max_length=10,
        choices=StockCondition.choices,
        default=StockCondition.GOOD,
    )

    movement_type = models.CharField(
        max_length=30,
        choices=MovementType.choices,
    )

    quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
    )

    balance_before = models.DecimalField(
        max_digits=14,
        decimal_places=3,
    )

    balance_after = models.DecimalField(
        max_digits=14,
        decimal_places=3,
    )

    # Transfer event references

    transfer_dispatch_item = models.ForeignKey(
        "inventory.StockTransferDispatchItem",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_movements",
    )

    transfer_receipt_item = models.ForeignKey(
        "inventory.StockTransferReceiptItem",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_movements",
    )

    transfer_return_item = models.ForeignKey(
        "inventory.StockTransferReturnItem",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_movements",
    )

    transfer_return_receipt_item = models.ForeignKey(
        "inventory.StockTransferReturnReceiptItem",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_movements",
    )

    reference = models.CharField(
        max_length=100,
        blank=True,
    )

    notes = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inventory_movements",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    objects = ImmutableStockMovementManager()

    class Meta:
        ordering = ["-created_at", "-pk"]

        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="stock_movement_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(balance_before__gte=0),
                name="movement_balance_before_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(balance_after__gte=0),
                name="movement_balance_after_non_negative",
            ),

            # Exactly zero or one transfer event reference.

            models.CheckConstraint(
                condition=(
                    Q(
                        transfer_dispatch_item__isnull=True,
                        transfer_receipt_item__isnull=True,
                        transfer_return_item__isnull=True,
                        transfer_return_receipt_item__isnull=True,
                    )
                    | Q(
                        transfer_dispatch_item__isnull=False,
                        transfer_receipt_item__isnull=True,
                        transfer_return_item__isnull=True,
                        transfer_return_receipt_item__isnull=True,
                    )
                    | Q(
                        transfer_dispatch_item__isnull=True,
                        transfer_receipt_item__isnull=False,
                        transfer_return_item__isnull=True,
                        transfer_return_receipt_item__isnull=True,
                    )
                    | Q(
                        transfer_dispatch_item__isnull=True,
                        transfer_receipt_item__isnull=True,
                        transfer_return_item__isnull=False,
                        transfer_return_receipt_item__isnull=True,
                    )
                    | Q(
                        transfer_dispatch_item__isnull=True,
                        transfer_receipt_item__isnull=True,
                        transfer_return_item__isnull=True,
                        transfer_return_receipt_item__isnull=False,
                    )
                ),
                name="stock_movement_single_transfer_event",
            ),

            # Dispatch: good stock leaves Head Office.

            models.CheckConstraint(
                condition=(
                    Q(transfer_dispatch_item__isnull=True)
                    | Q(
                        movement_type="transfer_out",
                        stock_condition="good",
                    )
                ),
                name="dispatch_movement_type_condition",
            ),

            # Branch receipt: good or damaged stock enters.

            models.CheckConstraint(
                condition=(
                    Q(transfer_receipt_item__isnull=True)
                    | Q(movement_type="transfer_in")
                ),
                name="receipt_movement_type",
            ),

            # Branch damaged return: damaged stock leaves.

            models.CheckConstraint(
                condition=(
                    Q(transfer_return_item__isnull=True)
                    | Q(
                        movement_type="transfer_out",
                        stock_condition="damaged",
                    )
                ),
                name="return_movement_type_condition",
            ),

            # HO return receipt: damaged stock enters.

            models.CheckConstraint(
                condition=(
                    Q(transfer_return_receipt_item__isnull=True)
                    | Q(
                        movement_type="transfer_in",
                        stock_condition="damaged",
                    )
                ),
                name="return_receipt_movement_type",
            ),

            # One movement per dispatch item.

            models.UniqueConstraint(
                fields=["transfer_dispatch_item"],
                condition=Q(
                    transfer_dispatch_item__isnull=False,
                ),
                name="unique_movement_per_dispatch_item",
            ),

            # One GOOD and one DAMAGED movement per receipt item.

            models.UniqueConstraint(
                fields=[
                    "transfer_receipt_item",
                    "stock_condition",
                ],
                condition=Q(
                    transfer_receipt_item__isnull=False,
                ),
                name="unique_movement_per_receipt_condition",
            ),

            # One movement per return item.

            models.UniqueConstraint(
                fields=["transfer_return_item"],
                condition=Q(
                    transfer_return_item__isnull=False,
                ),
                name="unique_movement_per_return_item",
            ),

            # One movement per HO return receipt item.

            models.UniqueConstraint(
                fields=["transfer_return_receipt_item"],
                condition=Q(
                    transfer_return_receipt_item__isnull=False,
                ),
                name="unique_movement_per_return_receipt_item",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError(
                "Existing stock movements cannot be modified."
            )

        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Stock movements cannot be deleted."
        )

    def __str__(self):
        return (
            f"{self.stock_balance.product.stock_no} - "
            f"{self.get_movement_type_display()} - "
            f"{self.quantity}"
        )


# ==================================================
# TRANSFER EVENT IMMUTABILITY
# ==================================================

class ImmutableTransferQuerySet(models.QuerySet):

    def update(self, **kwargs):
        raise ValidationError(
            "Transfer history cannot be updated."
        )

    def delete(self):
        raise ValidationError(
            "Transfer history cannot be deleted."
        )

    def bulk_create(self, *args, **kwargs):
        raise ValidationError(
            "Create transfer records through transfer services."
        )

    def bulk_update(self, *args, **kwargs):
        raise ValidationError(
            "Transfer history cannot be bulk updated."
        )


class ImmutableTransferManager(
    models.Manager.from_queryset(ImmutableTransferQuerySet)
):
    pass


class ImmutableTransferEventModel(models.Model):
    """
    Base class for append-only transfer event records.
    """

    objects = ImmutableTransferManager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError(
                "Existing transfer history cannot be modified."
            )

        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Transfer history cannot be deleted."
        )


# ==================================================
# STOCK TRANSFER ORDER
# ==================================================

class StockTransfer(models.Model):
    """
    Branch-to-Head-Office stock order.
    Creating an order does not change stock.
    """

    class Status(models.TextChoices):
        ORDERED = "ordered", "Ordered"
        PARTIALLY_DISPATCHED = "partially_dispatched", "Partially Dispatched"
        DISPATCHED = "dispatched", "Dispatched"
        PARTIALLY_RECEIVED = "partially_received", "Partially Received"
        RECEIVED = "received", "Received"
        CANCELLED = "cancelled", "Cancelled"

    source_business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="outgoing_stock_transfers",
    )

    destination_business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="incoming_stock_transfers",
    )

    reference = models.CharField(
        max_length=100,
        unique=True,
    )

    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.ORDERED,
    )

    notes = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_stock_transfers",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def clean(self):
        super().clean()

        if not self.source_business_id or not self.destination_business_id:
            return

        source = self.source_business
        destination = self.destination_business

        if source.business_type != "head_office":
            raise ValidationError({
                "source_business": (
                    "Transfer source must be a Head Office."
                )
            })

        if destination.business_type != "branch":
            raise ValidationError({
                "destination_business": (
                    "Transfer destination must be a Branch."
                )
            })

        if (
            destination.parent_business_id != source.pk
            or destination.company_id != source.company_id
        ):
            raise ValidationError(
                "Transfer must be between a Head Office and its own Branch."
            )

    def __str__(self):
        return self.reference


class StockTransferItem(models.Model):
    """
    Products and quantities requested by a Branch.
    """

    transfer = models.ForeignKey(
        StockTransfer,
        on_delete=models.PROTECT,
        related_name="items",
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="transfer_order_items",
    )

    ordered_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["transfer", "product"],
                name="unique_product_per_stock_transfer",
            ),
            models.CheckConstraint(
                condition=Q(ordered_quantity__gt=0),
                name="transfer_order_quantity_positive",
            ),
        ]

    def clean(self):
        super().clean()

        if not self.transfer_id or not self.product_id:
            return

        if (
            self.product.owner_business_id
            != self.transfer.source_business_id
        ):
            raise ValidationError({
                "product": (
                    "Product must belong to the source Head Office."
                )
            })

    def __str__(self):
        return (
            f"{self.product.stock_no} - "
            f"{self.ordered_quantity}"
        )


# ==================================================
# DISPATCH
# ==================================================

class StockTransferDispatch(ImmutableTransferEventModel):
    """
    Each dispatch batch from Head Office to Branch.
    """

    transfer = models.ForeignKey(
        StockTransfer,
        on_delete=models.PROTECT,
        related_name="dispatches",
    )

    reference = models.CharField(
        max_length=100,
        unique=True,
    )

    notes = models.TextField(blank=True)

    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="stock_transfer_dispatches",
    )

    dispatched_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.reference


class StockTransferDispatchItem(ImmutableTransferEventModel):

    dispatch = models.ForeignKey(
        StockTransferDispatch,
        on_delete=models.PROTECT,
        related_name="items",
    )

    transfer_item = models.ForeignKey(
        StockTransferItem,
        on_delete=models.PROTECT,
        related_name="dispatch_items",
    )

    quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["dispatch", "transfer_item"],
                name="unique_transfer_item_per_dispatch",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="transfer_dispatch_quantity_positive",
            ),
        ]

    def __str__(self):
        return (
            f"{self.transfer_item.product.stock_no} - "
            f"{self.quantity}"
        )


# ==================================================
# BRANCH RECEIPT
# ==================================================

class StockTransferReceipt(ImmutableTransferEventModel):
    """
    Each Branch receipt batch.
    """

    transfer = models.ForeignKey(
        StockTransfer,
        on_delete=models.PROTECT,
        related_name="receipts",
    )

    reference = models.CharField(
        max_length=100,
        unique=True,
    )

    notes = models.TextField(blank=True)

    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="stock_transfer_receipts",
    )

    received_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.reference


class StockTransferReceiptItem(ImmutableTransferEventModel):
    """
    Good and damaged quantities received by the Branch.
    """

    receipt = models.ForeignKey(
        StockTransferReceipt,
        on_delete=models.PROTECT,
        related_name="items",
    )

    transfer_item = models.ForeignKey(
        StockTransferItem,
        on_delete=models.PROTECT,
        related_name="receipt_items",
    )

    good_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=0,
    )

    damaged_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=0,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["receipt", "transfer_item"],
                name="unique_transfer_item_per_receipt",
            ),
            models.CheckConstraint(
                condition=Q(good_quantity__gte=0),
                name="transfer_receipt_good_quantity_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(damaged_quantity__gte=0),
                name="transfer_receipt_damaged_quantity_non_negative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(good_quantity__gt=0)
                    | Q(damaged_quantity__gt=0)
                ),
                name="transfer_receipt_quantity_positive",
            ),
        ]

    def __str__(self):
        return self.transfer_item.product.stock_no


# ==================================================
# DAMAGED STOCK RETURN
# ==================================================

class StockTransferReturn(ImmutableTransferEventModel):
    """
    A Branch return batch for damaged goods.
    """

    transfer = models.ForeignKey(
        StockTransfer,
        on_delete=models.PROTECT,
        related_name="returns",
    )

    reference = models.CharField(
        max_length=100,
        unique=True,
    )

    notes = models.TextField(blank=True)

    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="stock_transfer_returns",
    )

    dispatched_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.reference


class StockTransferReturnItem(ImmutableTransferEventModel):

    return_batch = models.ForeignKey(
        StockTransferReturn,
        on_delete=models.PROTECT,
        related_name="items",
    )

    transfer_item = models.ForeignKey(
        StockTransferItem,
        on_delete=models.PROTECT,
        related_name="return_items",
    )

    quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["return_batch", "transfer_item"],
                name="unique_transfer_item_per_return",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="transfer_return_quantity_positive",
            ),
        ]

    def __str__(self):
        return (
            f"{self.transfer_item.product.stock_no} - "
            f"{self.quantity}"
        )


# ==================================================
# RETURN RECEIPT AT HEAD OFFICE
# ==================================================

class StockTransferReturnReceipt(ImmutableTransferEventModel):
    """
    Head Office confirmation that returned damaged goods physically arrived.

    One return batch receives one complete HO receipt.
    """

    return_batch = models.OneToOneField(
        StockTransferReturn,
        on_delete=models.PROTECT,
        related_name="receipt",
    )

    reference = models.CharField(
        max_length=100,
        unique=True,
    )

    notes = models.TextField(blank=True)

    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="stock_transfer_return_receipts",
    )

    received_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.reference


class StockTransferReturnReceiptItem(ImmutableTransferEventModel):
    """
    Quantity actually received by Head Office for each returned product.
    """

    receipt = models.ForeignKey(
        StockTransferReturnReceipt,
        on_delete=models.PROTECT,
        related_name="items",
    )

    return_item = models.OneToOneField(
        StockTransferReturnItem,
        on_delete=models.PROTECT,
        related_name="receipt_item",
    )

    received_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(received_quantity__gt=0),
                name="return_receipt_quantity_positive",
            ),
        ]

    def __str__(self):
        return (
            f"{self.return_item.transfer_item.product.stock_no} - "
            f"{self.received_quantity}"
        )


# ==================================================
# HEAD OFFICE RETURN REVIEW
# ==================================================

class StockTransferReturnReview(ImmutableTransferEventModel):
    """
    HO inspection and response after physically receiving returned goods.
    """

    class Decision(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        PARTIALLY_ACCEPTED = (
            "partially_accepted",
            "Partially Accepted",
        )

    return_receipt = models.OneToOneField(
        StockTransferReturnReceipt,
        on_delete=models.PROTECT,
        related_name="review",
    )

    decision = models.CharField(
        max_length=30,
        choices=Decision.choices,
    )

    remarks = models.TextField()

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="stock_transfer_return_reviews",
    )

    reviewed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return (
            f"{self.return_receipt.reference} - "
            f"{self.get_decision_display()}"
        )


class StockTransferReturnReviewItem(ImmutableTransferEventModel):
    """
    Per-product inspection result.
    """

    review = models.ForeignKey(
        StockTransferReturnReview,
        on_delete=models.PROTECT,
        related_name="items",
    )

    receipt_item = models.ForeignKey(
        StockTransferReturnReceiptItem,
        on_delete=models.PROTECT,
        related_name="review_items",
    )

    accepted_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=0,
    )

    rejected_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=0,
    )

    remarks = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["review", "receipt_item"],
                name="unique_receipt_item_per_return_review",
            ),
            models.CheckConstraint(
                condition=Q(accepted_quantity__gte=0),
                name="return_review_accepted_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(rejected_quantity__gte=0),
                name="return_review_rejected_non_negative",
            ),
        ]

    def __str__(self):
        return (
            self.receipt_item.return_item.transfer_item.product.stock_no
        )


# ==================================================
# LOCAL PRODUCT
# ==================================================

class LocalProduct(models.Model):
    """
    Business-specific reusable sales-only product.

    Local products do not create or affect inventory stock.
    """

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="local_products",
    )

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    brand = models.CharField(max_length=100, blank=True)
    unit = models.CharField(max_length=20, default="pcs")

    purchase_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
    )

    sale_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
    )

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                models.functions.Lower("name"),
                "business",
                name="unique_local_product_name_per_business_ci",
            ),
            models.CheckConstraint(
                condition=Q(purchase_price__gte=0),
                name="local_product_purchase_price_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(sale_price__gte=0),
                name="local_product_sale_price_non_negative",
            ),
        ]

    def __str__(self):
        return f"{self.name} - {self.business.name}"
