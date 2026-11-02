from django.conf import settings
from django.db import models
from django.db.models import Q


class Expense(models.Model):
    """
    Money the business spent. Salary payments from the payroll app and
    supplier payments from the purchases app appear here automatically
    (categories ``salary`` and ``purchase``, read-only); other expenses are
    entered by the owner. Nothing is deleted: a wrong expense is voided.
    """

    class Category(models.TextChoices):
        SALARY = "salary", "Salary"
        PURCHASE = "purchase", "Purchase"
        RENT = "rent", "Rent"
        UTILITIES = "utilities", "Utilities"
        TRANSPORT = "transport", "Transport"
        MARKETING = "marketing", "Marketing"
        MAINTENANCE = "maintenance", "Maintenance"
        OTHER = "other", "Other"

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        BANK = "bank", "Bank transfer"
        ONLINE = "online", "Online"
        CHEQUE = "cheque", "Cheque"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        VOID = "void", "Void"

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="expenses",
    )
    category = models.CharField(max_length=20, choices=Category.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    expense_date = models.DateField()
    description = models.CharField(max_length=200, blank=True)
    payment_method = models.CharField(
        max_length=10, choices=Method.choices, blank=True
    )
    reference = models.CharField(max_length=100, blank=True)

    # Set only for salary expenses created from a payroll payment.
    payroll_payment = models.OneToOneField(
        "payroll.PayrollPayment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="expense",
    )

    # Set only for purchase expenses created from a supplier payment.
    purchase_payment = models.OneToOneField(
        "purchases.PurchasePayment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="expense",
    )

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
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-expense_date", "-id"]
        indexes = [
            models.Index(fields=["business", "status", "expense_date"]),
            models.Index(fields=["business", "category"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="expense_amount_positive",
            ),
            models.CheckConstraint(
                condition=Q(payroll_payment__isnull=True) | Q(category="salary"),
                name="expense_payroll_link_is_salary",
            ),
            models.CheckConstraint(
                condition=Q(purchase_payment__isnull=True) | Q(category="purchase"),
                name="expense_purchase_link_is_purchase",
            ),
        ]

    def __str__(self):
        return f"{self.category} {self.amount} on {self.expense_date}"


class Income(models.Model):
    """
    Money the business received. Every customer payment on a sale appears
    here automatically (read-only, in the category of its sale); other income
    is entered by the owner. Nothing is deleted: a wrong entry is voided, and
    voiding a sale payment voids its income.
    """

    class Category(models.TextChoices):
        STOCK_SALE = "stock_sale", "Stock sale"
        BIKE_SALE = "bike_sale", "Bike sale"
        MAINTENANCE = "maintenance", "Maintenance"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        VOID = "void", "Void"

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="incomes",
    )
    category = models.CharField(max_length=20, choices=Category.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    income_date = models.DateField()
    description = models.CharField(max_length=200, blank=True)
    payment_method = models.CharField(
        max_length=10, choices=Expense.Method.choices, blank=True
    )
    reference = models.CharField(max_length=100, blank=True)

    # Set only for income that mirrors a customer payment on a sale.
    sale_payment = models.OneToOneField(
        "sales.SalePayment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="income",
    )

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
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-income_date", "-id"]
        indexes = [
            models.Index(fields=["business", "status", "income_date"]),
            models.Index(fields=["business", "category"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="income_amount_positive",
            ),
            models.CheckConstraint(
                condition=Q(sale_payment__isnull=True) | ~Q(category="other"),
                name="income_sale_link_is_a_sale_category",
            ),
        ]

    def __str__(self):
        return f"{self.category} {self.amount} on {self.income_date}"
