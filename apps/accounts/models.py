from django.contrib.auth.models import AbstractUser, UserManager
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower


class VGSyncUserManager(UserManager):
    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields["role"] = "super_admin"
        extra_fields["company"] = None
        extra_fields["business"] = None
        extra_fields["position"] = None
        extra_fields["is_staff"] = True
        extra_fields["is_superuser"] = True

        return super().create_superuser(
            username=username,
            email=email,
            password=password,
            **extra_fields,
        )


class Position(models.Model):
    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="positions",
    )
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    department = models.CharField(max_length=100, blank=True)
    # Which preset it started from. Information only: editing a preset never
    # changes a Position, and a Position can be edited freely afterwards.
    preset = models.CharField(max_length=40, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                "business",
                name="unique_position_name_per_business",
            ),
        ]

    def __str__(self):
        return f"{self.name} - {self.business.name}"


class PositionPermission(models.Model):

    class Module(models.TextChoices):
        INVENTORY = "inventory", "Inventory"
        PURCHASES = "purchases", "Purchases"
        SALES = "sales", "Sales"
        BIKE_SALES = "bike_sales", "Bike Sales"
        ORDERS = "orders", "Orders"
        STAFF = "staff", "Staff & Payroll"
        FINANCE = "finance", "Finance"
        DASHBOARD = "dashboard", "Dashboard"
        REMINDERS = "reminders", "Reminders"
        REPORTS = "reports", "Reports"

    position = models.ForeignKey(
        Position,
        on_delete=models.CASCADE,
        related_name="permissions",
    )
    module = models.CharField(
        max_length=30,
        choices=Module.choices,
    )
    can_view = models.BooleanField(default=False)
    can_create = models.BooleanField(default=False)
    can_update = models.BooleanField(default=False)
    can_delete = models.BooleanField(default=False)
    # Finer actions beyond the four flags, e.g. ["cancel", "approve"].
    actions = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["module"]
        constraints = [
            models.UniqueConstraint(
                fields=["position", "module"],
                name="unique_position_module_permission",
            ),
        ]

    def __str__(self):
        return f"{self.position.name} - {self.get_module_display()}"


class StaffPermissionOverride(models.Model):
    """
    One person's exception to their Position: allow or deny one permission
    code. No row means "inherited" (use the Position).
    """

    class Effect(models.TextChoices):
        ALLOW = "allow", "Allowed"
        DENY = "deny", "Denied"

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="permission_overrides",
    )
    permission = models.CharField(max_length=60)
    effect = models.CharField(max_length=10, choices=Effect.choices)
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["permission"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "permission"],
                name="unique_override_per_user_permission",
            ),
        ]

    def __str__(self):
        from .rbac import code_label

        who = self.user.get_full_name() or self.user.username

        return f"{who}: {self.get_effect_display()} {code_label(self.permission)}"


class User(AbstractUser):

    class Role(models.TextChoices):
        SUPER_ADMIN = "super_admin", "Super Admin"
        OWNER = "owner", "Owner"
        STAFF = "staff", "Staff"

    # Username is unique within its tenant, not globally.
    username = models.CharField(
        max_length=150,
        unique=False,
        validators=[UnicodeUsernameValidator()],
        error_messages={
            "unique": "A user with that username already exists."
        },
        help_text=(
            "Required. 150 characters or fewer. "
            "Letters, digits and @/./+/-/_ only."
        ),
        verbose_name="username",
    )

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.STAFF,
    )

    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.PROTECT,
        related_name="users",
        null=True,
        blank=True,
    )

    business = models.ForeignKey(
        "businesses.Business",
        on_delete=models.PROTECT,
        related_name="users",
        null=True,
        blank=True,
    )

    # Internal tenant reference:
    # Company users and users of that company's branches share
    # the same username namespace.
    tenant_company = models.ForeignKey(
        "companies.Company",
        on_delete=models.PROTECT,
        related_name="tenant_users",
        null=True,
        blank=True,
        editable=False,
    )

    position = models.ForeignKey(
        Position,
        on_delete=models.PROTECT,
        related_name="users",
        null=True,
        blank=True,
    )

    phone = models.CharField(max_length=20, blank=True)

    must_change_password = models.BooleanField(default=False)

    objects = VGSyncUserManager()

    class Meta:
        ordering = ["username"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        role="super_admin",
                        company__isnull=True,
                        business__isnull=True,
                    )
                    |
                    Q(
                        role__in=["owner", "staff"],
                        company__isnull=False,
                        business__isnull=True,
                    )
                    |
                    Q(
                        role__in=["owner", "staff"],
                        company__isnull=True,
                        business__isnull=False,
                    )
                ),
                name="valid_user_role_scope",
            ),

            # Company users and company-business users share
            # one username namespace.
            models.UniqueConstraint(
                fields=["username", "tenant_company"],
                condition=Q(tenant_company__isnull=False),
                name="unique_username_per_company",
            ),

            # Independent business users have their own namespace.
            models.UniqueConstraint(
                fields=["username", "business"],
                condition=Q(
                    tenant_company__isnull=True,
                    business__isnull=False,
                ),
                name="unique_username_per_independent_business",
            ),

            # Super Admin usernames remain unique.
            models.UniqueConstraint(
                fields=["username"],
                condition=Q(role="super_admin"),
                name="unique_super_admin_username",
            ),
        ]

    def save(self, *args, **kwargs):
        # Determine the tenant company from the user's assignment.
        if self.company_id:
            self.tenant_company_id = self.company_id

        elif self.business_id:
            self.tenant_company_id = self.business.company_id

        else:
            self.tenant_company_id = None

        # Keep tenant_company updated when save(update_fields=...)
        # is used and the company/business assignment changes.
        update_fields = kwargs.get("update_fields")

        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {
                "tenant_company"
            }

        super().save(*args, **kwargs)

    @property
    def scope(self):
        if self.role == self.Role.SUPER_ADMIN:
            return "platform"

        if self.company_id:
            return "company"

        if self.business_id:
            return "business"

        return None

    def clean(self):
        super().clean()

        errors = {}

        # Super Admin must not belong to any tenant.
        if self.role == self.Role.SUPER_ADMIN:

            if self.company_id or self.business_id or self.position_id:
                errors["company"] = (
                    "Super Admin cannot belong to a company, "
                    "business, or position."
                )

        elif self.role in [self.Role.OWNER, self.Role.STAFF]:

            has_company = bool(self.company_id)
            has_business = bool(self.business_id)

            if has_company == has_business:
                errors["company"] = (
                    "Assign the user to exactly one company or business."
                )

            # Only Staff can have a position.
            if self.role != self.Role.STAFF and self.position_id:
                errors["position"] = (
                    "Only Staff users can be assigned a position."
                )

            # Validate Staff position scope.
            if self.role == self.Role.STAFF and self.position_id:

                position_business = self.position.business

                # Company Staff positions must belong to Head Office.
                if self.company_id:

                    if (
                        position_business.business_type != "head_office"
                        or position_business.company_id != self.company_id
                    ):
                        errors["position"] = (
                            "Company Staff must have a position belonging "
                            "to their company's Head Office."
                        )

                # Business Staff positions must belong to their business.
                elif self.business_id:

                    if position_business.id != self.business_id:
                        errors["position"] = (
                            "Business Staff must have a position belonging "
                            "to their assigned business."
                        )

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.username
