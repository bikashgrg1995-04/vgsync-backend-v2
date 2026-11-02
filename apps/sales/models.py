"""
Selling stock to customers.

A sale starts as a **draft** (editable, no effect on stock). When it is
**confirmed**, stock goes down once, through the inventory stock service, an
invoice number is given, and the sale is locked. A draft that is not needed
is **cancelled**. Money received is recorded as separate payments, so what
is paid and what the customer still owes (credit to receive) is always
worked out from them.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q

MONEY = {"max_digits": 12, "decimal_places": 2}
ZERO = Decimal("0.00")


class Customer(models.Model):
    """Someone a business sells to. Customers belong to one business."""

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="customers",
    )
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.CharField(max_length=255, blank=True)
    # The customer's PAN or VAT number, for tax invoices.
    pan_number = models.CharField(max_length=30, blank=True)
    notes = models.TextField(blank=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["business", "name"])]
        constraints = [
            # Two people can share a name, but not a phone number.
            models.UniqueConstraint(
                fields=["business", "phone"],
                condition=~Q(phone=""),
                name="unique_customer_phone_per_business",
            ),
        ]

    def __str__(self):
        return self.name


class Sale(models.Model):
    """
    One sale. Every money figure is calculated by the server from the lines
    (see ``services.calculate_totals``):

        subtotal  = sum of line totals
        taxable   = subtotal - discount
        vat       = taxable x vat_rate / 100
        net total = taxable + vat
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        CONFIRMED = "confirmed", "Confirmed"
        CANCELLED = "cancelled", "Cancelled"

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="sales",
    )
    # 1, 2, 3 ... within the business from the day the draft is made;
    # shown as SAL-000001.
    number = models.PositiveIntegerField()
    # Given only when the sale is confirmed, so cancelled drafts never leave
    # a gap in the invoice sequence; shown as INV-000001.
    invoice_number = models.PositiveIntegerField(null=True, blank=True)
    # Empty for a walk-in sale.
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sales",
    )
    # Optional name to print for a walk-in customer.
    walk_in_name = models.CharField(max_length=150, blank=True)
    sale_date = models.DateField()

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.DRAFT,
    )

    subtotal = models.DecimalField(**MONEY, default=ZERO)
    discount_amount = models.DecimalField(**MONEY, default=ZERO)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO)
    vat_amount = models.DecimalField(**MONEY, default=ZERO)
    net_total = models.DecimalField(**MONEY, default=ZERO)

    notes = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
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
        ordering = ["-sale_date", "-number"]
        indexes = [
            models.Index(fields=["business", "status", "sale_date"]),
            models.Index(fields=["customer", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "number"],
                name="unique_sale_number_per_business",
            ),
            models.UniqueConstraint(
                fields=["business", "invoice_number"],
                condition=Q(invoice_number__isnull=False),
                name="unique_invoice_number_per_business",
            ),
            # Only a confirmed sale has an invoice number.
            models.CheckConstraint(
                condition=(
                    Q(status="confirmed", invoice_number__isnull=False)
                    | (~Q(status="confirmed") & Q(invoice_number__isnull=True))
                ),
                name="sale_invoice_number_only_when_confirmed",
            ),
            models.CheckConstraint(
                condition=(
                    Q(subtotal__gte=0)
                    & Q(discount_amount__gte=0)
                    & Q(discount_amount__lte=models.F("subtotal"))
                    & Q(vat_rate__gte=0)
                    & Q(vat_rate__lte=100)
                    & Q(vat_amount__gte=0)
                    & Q(net_total__gte=0)
                ),
                name="sale_amounts_valid",
            ),
        ]

    @property
    def sale_no(self):
        return f"SAL-{self.number:06d}"

    @property
    def invoice_no(self):
        if self.invoice_number is None:
            return None

        return f"INV-{self.invoice_number:06d}"

    def __str__(self):
        return self.sale_no


class SaleItem(models.Model):
    """One line of a sale."""

    sale = models.ForeignKey(
        Sale,
        on_delete=models.CASCADE,
        related_name="items",
    )
    product = models.ForeignKey(
        "inventory.Product",
        on_delete=models.PROTECT,
        related_name="sale_items",
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_price = models.DecimalField(**MONEY)
    line_total = models.DecimalField(**MONEY)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="sale_item_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gt=0) & Q(line_total__gt=0),
                name="sale_item_amounts_positive",
            ),
        ]

    def __str__(self):
        return f"{self.sale} - {self.product_id} x {self.quantity}"


class SalePayment(models.Model):
    """
    Money received from the customer for one sale. Several payments can be
    taken against one sale. A payment is never edited or deleted; a mistake
    is voided with a reason.
    """

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        BANK = "bank", "Bank transfer"
        ONLINE = "online", "Online"
        CHEQUE = "cheque", "Cheque"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        VOID = "void", "Void"

    sale = models.ForeignKey(
        Sale,
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
    received_by = models.ForeignKey(
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
            models.Index(fields=["sale", "status"]),
            models.Index(fields=["paid_on"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="sale_payment_amount_positive",
            ),
        ]

    def __str__(self):
        return f"{self.sale} received {self.amount}"
