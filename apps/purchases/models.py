"""
Buying stock from suppliers.

A purchase starts as a **draft** (editable, no effect on stock). When the
goods arrive it is **received**: stock goes up once, through the inventory
stock service, and the purchase is locked. A draft that is not needed is
**cancelled**. Money paid to the supplier is recorded as separate payments,
so how much is paid and still owed is always worked out from them.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

MONEY = {"max_digits": 12, "decimal_places": 2}
ZERO = Decimal("0.00")


class Supplier(models.Model):
    """Someone a business buys stock from. Suppliers belong to one business."""

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="suppliers",
    )
    name = models.CharField(max_length=200)
    contact_person = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.CharField(max_length=255, blank=True)
    # The supplier's PAN or VAT registration number, for bills.
    pan_number = models.CharField(max_length=30, blank=True)
    notes = models.TextField(blank=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                "business",
                name="unique_supplier_name_per_business",
            ),
        ]

    def __str__(self):
        return self.name


class Purchase(models.Model):
    """
    One purchase from one supplier.

    All money figures are calculated by the server from the lines (see
    ``services.calculate_totals``), in this order:

        subtotal   = sum of line totals
        taxable    = subtotal - discount
        vat        = taxable x vat_rate / 100
        net total  = taxable + vat + freight (freight is not taxed)
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        RECEIVED = "received", "Received"
        CANCELLED = "cancelled", "Cancelled"

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="purchases",
    )
    # 1, 2, 3 ... within the business; shown as PUR-000001.
    number = models.PositiveIntegerField()
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="purchases",
    )
    # The supplier's own bill or invoice number.
    bill_no = models.CharField(max_length=60, blank=True)
    purchase_date = models.DateField()

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.DRAFT,
    )

    subtotal = models.DecimalField(**MONEY, default=ZERO)
    discount_amount = models.DecimalField(**MONEY, default=ZERO)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO)
    vat_amount = models.DecimalField(**MONEY, default=ZERO)
    freight_amount = models.DecimalField(**MONEY, default=ZERO)
    net_total = models.DecimalField(**MONEY, default=ZERO)

    notes = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    received_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-purchase_date", "-number"]
        indexes = [
            models.Index(fields=["business", "status", "purchase_date"]),
            models.Index(fields=["supplier", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "number"],
                name="unique_purchase_number_per_business",
            ),
            # The same supplier bill cannot be entered twice.
            models.UniqueConstraint(
                fields=["business", "supplier", "bill_no"],
                condition=~Q(bill_no="") & ~Q(status="cancelled"),
                name="unique_live_supplier_bill",
            ),
            models.CheckConstraint(
                condition=(
                    Q(subtotal__gte=0)
                    & Q(discount_amount__gte=0)
                    & Q(discount_amount__lte=models.F("subtotal"))
                    & Q(vat_rate__gte=0)
                    & Q(vat_rate__lte=100)
                    & Q(vat_amount__gte=0)
                    & Q(freight_amount__gte=0)
                    & Q(net_total__gte=0)
                ),
                name="purchase_amounts_valid",
            ),
        ]

    @property
    def purchase_no(self):
        return f"PUR-{self.number:06d}"

    def __str__(self):
        return self.purchase_no


class PurchaseItem(models.Model):
    """One line of a purchase."""

    purchase = models.ForeignKey(
        Purchase,
        on_delete=models.CASCADE,
        related_name="items",
    )
    product = models.ForeignKey(
        "inventory.Product",
        on_delete=models.PROTECT,
        related_name="purchase_items",
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_price = models.DecimalField(**MONEY)
    line_total = models.DecimalField(**MONEY)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="purchase_item_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gt=0) & Q(line_total__gt=0),
                name="purchase_item_amounts_positive",
            ),
        ]

    def __str__(self):
        return f"{self.purchase} - {self.product_id} x {self.quantity}"


class PurchasePayment(models.Model):
    """
    Money paid to the supplier for one purchase. Several payments can be made
    against one purchase. A payment is never edited or deleted; a mistake is
    voided with a reason.
    """

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        BANK = "bank", "Bank transfer"
        ONLINE = "online", "Online"
        CHEQUE = "cheque", "Cheque"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        VOID = "void", "Void"

    purchase = models.ForeignKey(
        Purchase,
        on_delete=models.PROTECT,
        related_name="payments",
    )
    amount = models.DecimalField(**MONEY)
    paid_on = models.DateField()
    method = models.CharField(max_length=10, choices=Method.choices)
    reference = models.CharField(max_length=100, blank=True)
    notes = models.CharField(max_length=200, blank=True)

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-paid_on", "-id"]
        indexes = [
            models.Index(fields=["purchase", "status"]),
            models.Index(fields=["paid_on"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="purchase_payment_amount_positive",
            ),
        ]

    def __str__(self):
        return f"{self.purchase} paid {self.amount} on {self.paid_on}"
