import django_filters
from django import forms

from apps.inventory.models import StockMovement


class StockMovementFilterForm(forms.Form):

    def clean(self):
        cleaned_data = super().clean()

        date_from = cleaned_data.get("date_from")
        date_to = cleaned_data.get("date_to")

        if date_from and date_to and date_from > date_to:
            raise forms.ValidationError(
                "date_from cannot be later than date_to."
            )

        return cleaned_data


class StockMovementFilter(django_filters.FilterSet):

    product_id = django_filters.NumberFilter(
        field_name="stock_balance__product_id",
    )

    movement_type = django_filters.ChoiceFilter(
        choices=StockMovement.MovementType.choices,
    )

    stock_condition = django_filters.ChoiceFilter(
        choices=StockMovement.StockCondition.choices,
    )

    date_from = django_filters.DateFilter(
        field_name="created_at",
        lookup_expr="date__gte",
    )

    date_to = django_filters.DateFilter(
        field_name="created_at",
        lookup_expr="date__lte",
    )

    class Meta:
        model = StockMovement
        fields = []
        form = StockMovementFilterForm