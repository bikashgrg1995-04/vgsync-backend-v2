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
from django.db.models.functions import Lower

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


class BikeUnit(models.Model):
    """
    One physical bike, with its own chassis and engine number. A bike sale
    sells units, never just a quantity. The unit belongs to the business
    that holds it in stock and is marked sold when the sale is confirmed.
    Registering a unit does not change stock; stock comes from purchases or
    transfers, and there can never be more units in stock than the stock
    balance of the model.
    """

    class Status(models.TextChoices):
        IN_STOCK = "in_stock", "In stock"
        SOLD = "sold", "Sold"

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="bike_units",
    )
    product = models.ForeignKey(
        "inventory.Product",
        on_delete=models.PROTECT,
        related_name="bike_units",
    )
    chassis_no = models.CharField(max_length=60)
    engine_no = models.CharField(max_length=60, blank=True)
    color = models.CharField(max_length=40, blank=True)
    notes = models.CharField(max_length=200, blank=True)

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.IN_STOCK,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["business", "product", "status"])]
        constraints = [
            # A chassis number is one physical bike, across all businesses.
            models.UniqueConstraint(
                Lower("chassis_no"), name="unique_bike_chassis_no"
            ),
            models.UniqueConstraint(
                Lower("engine_no"),
                condition=~Q(engine_no=""),
                name="unique_bike_engine_no",
            ),
        ]

    def __str__(self):
        return self.chassis_no


class Sale(models.Model):
    """
    One sale. ``sale_type`` says what is sold:

    * ``stock``: products from stock.
    * ``bike``: bike units, paid in full or by EMI.
    * ``maintenance``: stock parts and service charges for a customer's bike.

    Every money figure is calculated by the server from the lines
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

    class SaleType(models.TextChoices):
        STOCK = "stock", "Stock sale"
        BIKE = "bike", "Bike sale"
        MAINTENANCE = "maintenance", "Maintenance"

    class PaymentMode(models.TextChoices):
        FULL = "full", "Full payment"
        EMI = "emi", "EMI"

    sale_type = models.CharField(
        max_length=12, choices=SaleType.choices, default=SaleType.STOCK
    )

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.DRAFT,
    )

    # Bike sales only. With EMI the customer pays ``down_payment`` and then
    # ``emi_months`` monthly instalments of ``emi_amount``. Whatever that
    # adds up to above the net total is the ``finance_charge``.
    payment_mode = models.CharField(
        max_length=5, choices=PaymentMode.choices, default=PaymentMode.FULL
    )
    down_payment = models.DecimalField(**MONEY, default=ZERO)
    emi_months = models.PositiveSmallIntegerField(null=True, blank=True)
    emi_amount = models.DecimalField(**MONEY, null=True, blank=True)
    first_emi_due = models.DateField(null=True, blank=True)
    finance_charge = models.DecimalField(**MONEY, default=ZERO)

    # When the customer promised to pay what is left (a credit sale).
    due_date = models.DateField(null=True, blank=True)

    # Maintenance only: the customer's bike.
    vehicle_chassis_no = models.CharField(max_length=60, blank=True)
    vehicle_reg_no = models.CharField(max_length=30, blank=True)

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
            # EMI is for bike sales, with a down payment, a term and an amount.
            models.CheckConstraint(
                condition=(
                    Q(
                        payment_mode="full",
                        down_payment=0,
                        finance_charge=0,
                        emi_months__isnull=True,
                        emi_amount__isnull=True,
                        first_emi_due__isnull=True,
                    )
                    | Q(
                        payment_mode="emi",
                        sale_type="bike",
                        down_payment__gte=0,
                        finance_charge__gte=0,
                        emi_months__gte=1,
                        emi_months__lte=120,
                        emi_amount__gt=0,
                        first_emi_due__isnull=False,
                    )
                ),
                name="sale_emi_fields_valid",
            ),
        ]

    @property
    def amount_due(self):
        """What the customer has to pay in all: net total plus finance charge."""
        return self.net_total + self.finance_charge

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
    # Empty for a service charge (maintenance), which takes nothing from stock.
    product = models.ForeignKey(
        "inventory.Product",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sale_items",
    )
    # What a service charge is for.
    description = models.CharField(max_length=200, blank=True)
    # Set on bike sales: the exact bike sold.
    bike_unit = models.ForeignKey(
        BikeUnit,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sale_items",
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_price = models.DecimalField(**MONEY)
    line_total = models.DecimalField(**MONEY)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(product__isnull=False) | ~Q(description=""),
                name="sale_item_has_product_or_description",
            ),
            models.CheckConstraint(
                condition=Q(bike_unit__isnull=True) | Q(product__isnull=False),
                name="sale_item_bike_has_product",
            ),
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


class EmiInstallment(models.Model):
    """
    One monthly instalment of an EMI sale, made when the sale is confirmed.
    Instalments are not paid one by one: payments go to the sale, and the
    money received after the down payment covers the instalments in order.
    """

    sale = models.ForeignKey(
        Sale,
        on_delete=models.CASCADE,
        related_name="installments",
    )
    number = models.PositiveSmallIntegerField()
    due_date = models.DateField()
    amount = models.DecimalField(**MONEY)

    class Meta:
        ordering = ["number"]
        constraints = [
            models.UniqueConstraint(
                fields=["sale", "number"], name="unique_installment_number"
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="installment_amount_positive"
            ),
        ]

    def __str__(self):
        return f"{self.sale} instalment {self.number}"


class Followup(models.Model):
    """
    A call or visit to make. They are created by the system:

    * ``maintenance_post``: 3 days after a maintenance sale, to ask whether
      any issue came up after the service. What is noted here (``has_issue``
      and the note) is shown on the 90-day follow-up.
    * ``maintenance_due``: 90 days after a maintenance sale.
    * ``emi_due``: one per EMI instalment, on its due date.
    * ``credit_due``: when a sale was left partly unpaid.

    A follow-up closes by itself once the money it was waiting for is
    received (``auto_closed``) and opens again if that payment is voided. It
    can also be marked done or skipped, with a note.
    """

    class Kind(models.TextChoices):
        MAINTENANCE_POST = "maintenance_post", "Post-service check (3 days)"
        MAINTENANCE_DUE = "maintenance_due", "Maintenance follow-up (90 days)"
        EMI_DUE = "emi_due", "EMI instalment"
        CREDIT_DUE = "credit_due", "Credit to receive"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DONE = "done", "Done"
        SKIPPED = "skipped", "Skipped"

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="followups",
    )
    sale = models.ForeignKey(
        Sale,
        on_delete=models.PROTECT,
        related_name="followups",
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="followups",
    )
    installment = models.OneToOneField(
        EmiInstallment,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="followup",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    due_date = models.DateField()
    # What is expected for an instalment or a credit; empty for maintenance.
    amount = models.DecimalField(**MONEY, null=True, blank=True)

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    auto_closed = models.BooleanField(default=False)
    completed_on = models.DateField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    note = models.CharField(max_length=300, blank=True)
    # Maintenance only: did the customer report a problem after the service?
    has_issue = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["due_date", "id"]
        indexes = [
            models.Index(fields=["business", "status", "due_date"]),
            models.Index(fields=["sale", "kind"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(kind="emi_due", installment__isnull=False)
                | (~Q(kind="emi_due") & Q(installment__isnull=True)),
                name="followup_installment_only_for_emi",
            ),
        ]

    def __str__(self):
        return f"{self.kind} {self.due_date}"


# ---------------------------------------------------------------------------
# Returns from customers
# ---------------------------------------------------------------------------

class SaleReturn(models.Model):
    """
    Goods a customer brought back from a confirmed sale: stock goods or the
    parts of a maintenance sale (a service charge and a bike are not
    returned). Returning puts the goods back into good stock and is worth
    the value of what came back, with its share
    of the discount and VAT (``goods_value``). That value first reduces what
    the customer still owes on the same sale (``applied_amount``); whatever
    is left is a **credit to pay** to the customer (``credit_amount``),
    kept apart from what the customer owes. It is used up by a refund, by
    applying it to another sale, or by giving other goods (see
    ``SaleCreditSettlement``).
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        VOID = "void", "Void"

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="sale_returns",
    )
    # 1, 2, 3 ... within the business; shown as SRT-000001.
    number = models.PositiveIntegerField()
    sale = models.ForeignKey(
        Sale,
        on_delete=models.PROTECT,
        related_name="returns",
    )
    # Empty for a walk-in sale, which is refunded straight away.
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="returns",
    )
    return_date = models.DateField()
    reason = models.TextField()
    notes = models.TextField(blank=True)
    # Taken off what the customer still owed on this sale.
    applied_amount = models.DecimalField(**MONEY, default=ZERO)
    # Left over after that: what is now owed back to the customer.
    credit_amount = models.DecimalField(**MONEY, default=ZERO)

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    created_by = models.ForeignKey(
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
        ordering = ["-return_date", "-number"]
        indexes = [
            models.Index(fields=["business", "status", "return_date"]),
            models.Index(fields=["customer", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "number"],
                name="unique_sale_return_number_per_business",
            ),
            models.CheckConstraint(
                condition=Q(credit_amount__gte=0) & Q(applied_amount__gte=0),
                name="sale_return_credit_non_negative",
            ),
        ]

    @property
    def return_no(self):
        return f"SRT-{self.number:06d}"

    def __str__(self):
        return self.return_no


class SaleReturnItem(models.Model):
    sale_return = models.ForeignKey(
        SaleReturn,
        on_delete=models.CASCADE,
        related_name="items",
    )
    sale_item = models.ForeignKey(
        SaleItem,
        on_delete=models.PROTECT,
        related_name="return_items",
    )
    product = models.ForeignKey(
        "inventory.Product",
        on_delete=models.PROTECT,
        related_name="sale_return_items",
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    # Returned goods always go back into good stock.
    stock_condition = models.CharField(max_length=10, default="good")
    # The value of these goods (with their share of discount and VAT).
    credit_amount = models.DecimalField(**MONEY, default=ZERO)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="sale_return_item_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(credit_amount__gte=0),
                name="sale_return_item_credit_non_negative",
            ),
        ]


class SaleCreditSettlement(models.Model):
    """
    Use of a customer's credit to pay. Every way reduces the credit:

    * ``refund``: money handed back to the customer. This is cash going out,
      so it reduces net income in the income summary.
    * ``adjustment``: the credit is set against what the customer still owes
      on another (non-EMI) sale. No cash moves.
    * ``replacement``: other goods are given instead of money. The credit is
      used up and the goods are **stock to give** until they are delivered;
      ``deliver`` then takes them out of stock.

    A walk-in return has no customer, so its refund is made together with the
    return (``sale_return`` is set). Never edited or deleted; a mistake is
    voided with a reason.
    """

    class Kind(models.TextChoices):
        REFUND = "refund", "Refund paid"
        ADJUSTMENT = "adjustment", "Set against a sale"
        REPLACEMENT = "replacement", "Replacement goods"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        VOID = "void", "Void"

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="sale_credit_settlements",
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="credit_settlements",
    )
    # Set for the refund of a walk-in return.
    sale_return = models.ForeignKey(
        SaleReturn,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="settlements",
    )
    kind = models.CharField(max_length=12, choices=Kind.choices)
    amount = models.DecimalField(**MONEY)
    settled_on = models.DateField()
    # Only for an adjustment: the sale the credit is set against.
    sale = models.ForeignKey(
        Sale,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="credit_settlements",
    )
    # Only for a refund.
    method = models.CharField(max_length=10, blank=True)
    # Only for a replacement: when the goods were handed over (empty while
    # they are still to be delivered).
    delivered_on = models.DateField(null=True, blank=True)
    delivered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    reference = models.CharField(max_length=100, blank=True)
    notes = models.CharField(max_length=200, blank=True)

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    created_by = models.ForeignKey(
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
        ordering = ["-settled_on", "-id"]
        indexes = [
            models.Index(fields=["business", "kind", "status"]),
            models.Index(fields=["customer", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="sale_credit_amount_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(kind="refund", sale__isnull=True) & ~Q(method="")
                    | Q(kind="adjustment", sale__isnull=False, method="")
                    | Q(kind="replacement", sale__isnull=True, method="")
                ),
                name="sale_credit_fields_match_kind",
            ),
            # Without a customer the only possible thing is the immediate
            # refund of a walk-in return.
            models.CheckConstraint(
                condition=Q(customer__isnull=False)
                | Q(kind="refund", sale_return__isnull=False),
                name="sale_credit_customer_or_walk_in_refund",
            ),
        ]

    def __str__(self):
        return f"{self.kind} {self.amount}"


class SaleCreditSettlementItem(models.Model):
    """Goods owed to a customer in place of money (a ``replacement``)."""

    settlement = models.ForeignKey(
        SaleCreditSettlement,
        on_delete=models.CASCADE,
        related_name="items",
    )
    product = models.ForeignKey(
        "inventory.Product",
        on_delete=models.PROTECT,
        related_name="+",
    )
    quantity = models.DecimalField(max_digits=12, decimal_places=3)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="sale_credit_item_quantity_positive",
            ),
            models.UniqueConstraint(
                fields=["settlement", "product"],
                name="unique_product_per_sale_replacement",
            ),
        ]

    def __str__(self):
        return f"{self.product} x {self.quantity}"
