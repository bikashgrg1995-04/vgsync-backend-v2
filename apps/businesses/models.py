from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class Business(models.Model):

    class BusinessType(models.TextChoices):
        HEAD_OFFICE = "head_office", "Head Office"
        BRANCH = "branch", "Branch"
        INDEPENDENT = "independent", "Independent"

    name = models.CharField(max_length=150)

    business_type = models.CharField(
        max_length=20,
        choices=BusinessType.choices,
    )

    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.PROTECT,
        related_name="businesses",
        null=True,
        blank=True,
    )

    parent_business = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="branches",
        null=True,
        blank=True,
    )

    business_code = models.CharField(
        max_length=50,
        unique=True,
    )

    address = models.TextField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)

    is_active = models.BooleanField(default=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_businesses",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Businesses"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        business_type="head_office",
                        company__isnull=False,
                        parent_business__isnull=True,
                    )
                    | models.Q(
                        business_type="branch",
                        company__isnull=False,
                        parent_business__isnull=False,
                    )
                    | models.Q(
                        business_type="independent",
                        company__isnull=True,
                        parent_business__isnull=True,
                    )
                ),
                name="valid_business_hierarchy",
            ),
        ]

    def clean(self):
        super().clean()

        errors = {}

        if self.business_type == self.BusinessType.HEAD_OFFICE:
            if not self.company_id:
                errors["company"] = "Head Office must belong to a company."

            if self.parent_business_id:
                errors["parent_business"] = (
                    "Head Office cannot have a parent business."
                )

        elif self.business_type == self.BusinessType.BRANCH:
            if not self.company_id:
                errors["company"] = "Branch must belong to a company."

            if not self.parent_business_id:
                errors["parent_business"] = (
                    "Branch must have a parent Head Office."
                )
            elif self.parent_business:
                if self.parent_business.business_type != self.BusinessType.HEAD_OFFICE:
                    errors["parent_business"] = (
                        "Branch must belong directly to a Head Office."
                    )
                elif self.parent_business.company_id != self.company_id:
                    errors["parent_business"] = (
                        "Branch and Head Office must belong to the same company."
                    )

        elif self.business_type == self.BusinessType.INDEPENDENT:
            if self.company_id:
                errors["company"] = (
                    "Independent business cannot belong to a company."
                )

            if self.parent_business_id:
                errors["parent_business"] = (
                    "Independent business cannot have a parent business."
                )

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.name