"""Cash and bank: what the books expect, and checking it against reality."""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.dates import parse_query_date
from apps.common.permissions import HasActiveBusinessContext, IsOwner

from .models import Expense, Income, OpeningBalance, Reconciliation
from .services import method_balance

METHODS = [m for m, _ in Expense.Method.choices]
MONEY = {"max_digits": 14, "decimal_places": 2}


class OwnerView(APIView):
    permission_classes = [IsAuthenticated, HasActiveBusinessContext, IsOwner]


def _money(value):
    return f"{value:.2f}"


def _reconciliation(row):
    return {
        "id": row.pk,
        "method": row.method,
        "as_of": row.as_of,
        "expected": _money(row.expected),
        "counted": _money(row.counted),
        "difference": _money(row.difference),
        "note": row.note,
        "adjusted": row.adjusted,
        "created_by": (row.created_by.get_full_name() or row.created_by.username) if row.created_by else "",
        "created_at": row.created_at,
    }


class BalancesView(OwnerView):
    """``GET ?as_of=`` — per method: opening, in, out, expected, and the last check."""

    def get(self, request):
        business = request.active_business
        as_of = parse_query_date(request.query_params.get("as_of")) or timezone.localdate()
        rows = []

        for method in METHODS:
            opening = OpeningBalance.objects.filter(business=business, method=method).first()
            figures = method_balance(business, method, max(as_of, opening.as_of) if opening else as_of)
            last = (
                Reconciliation.objects.filter(business=business, method=method)
                .select_related("created_by").first()
            )
            rows.append({
                "method": method,
                "label": dict(Expense.Method.choices)[method],
                "opening": _money(figures["opening"]),
                "opening_as_of": figures["opening_as_of"],
                "received": _money(figures["received"]),
                "paid": _money(figures["paid"]),
                "expected": _money(figures["expected"]),
                "last_check": _reconciliation(last) if last else None,
            })

        # Entries with no method cannot be placed in any balance.
        unplaced = (
            Income.objects.filter(business=business, status="active", payment_method="").count()
            + Expense.objects.filter(business=business, status="active", payment_method="").count()
        )

        return Response({"as_of": as_of, "methods": rows, "without_method": unplaced})


class OpeningBalanceSerializer(serializers.Serializer):
    amount = serializers.DecimalField(**MONEY)
    as_of = serializers.DateField()


class OpeningBalanceView(OwnerView):
    """``PUT <method>/`` — set what was held at the end of a date."""

    def put(self, request, method):
        if method not in METHODS:
            raise ValidationError({"method": f"Use one of: {', '.join(METHODS)}."})

        serializer = OpeningBalanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if serializer.validated_data["as_of"] > timezone.localdate():
            raise ValidationError({"as_of": "The date cannot be in the future."})

        row, _ = OpeningBalance.objects.update_or_create(
            business=request.active_business,
            method=method,
            defaults={**serializer.validated_data, "updated_by": request.user},
        )

        return Response({"method": method, "amount": _money(row.amount), "as_of": row.as_of})


class ReconciliationSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=Expense.Method.choices)
    as_of = serializers.DateField(required=False)
    counted = serializers.DecimalField(**MONEY)
    note = serializers.CharField(max_length=200, required=False, allow_blank=True)
    adjust = serializers.BooleanField(required=False, default=False)


class ReconciliationsView(OwnerView):
    """``GET`` the history (``?method=``); ``POST`` a new check."""

    def get(self, request):
        rows = Reconciliation.objects.filter(business=request.active_business).select_related("created_by")
        method = request.query_params.get("method")

        if method:
            rows = rows.filter(method=method)

        return Response([_reconciliation(r) for r in rows[:100]])

    @transaction.atomic
    def post(self, request):
        serializer = ReconciliationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        business = request.active_business
        as_of = data.get("as_of") or timezone.localdate()

        if as_of > timezone.localdate():
            raise ValidationError({"as_of": "The date cannot be in the future."})

        expected = method_balance(business, data["method"], as_of)["expected"]
        difference = data["counted"] - expected

        opening = OpeningBalance.objects.filter(business=business, method=data["method"]).first()

        if data["adjust"] and difference != 0 and opening and as_of <= opening.as_of:
            # Entries up to the opening date are already inside it, so an
            # entry dated then would change nothing.
            raise ValidationError(
                {"adjust": "That date is the opening balance date. Correct the opening balance instead, or check a later date."}
            )

        if data["adjust"] and difference != 0:
            # Write the gap into the books so they agree with what was counted.
            fields = {
                "business": business,
                "category": "other",
                "amount": abs(difference),
                "payment_method": data["method"],
                "description": "Reconciliation adjustment",
                "reference": f"Check on {as_of}",
                "created_by": request.user,
            }

            if difference > 0:
                Income.objects.create(income_date=as_of, **fields)
            else:
                Expense.objects.create(expense_date=as_of, **fields)

        row = Reconciliation.objects.create(
            business=business,
            method=data["method"],
            as_of=as_of,
            expected=expected,
            counted=data["counted"],
            difference=difference,
            note=data.get("note", ""),
            adjusted=bool(data["adjust"] and difference != 0),
            created_by=request.user,
        )

        return Response(_reconciliation(row), status=status.HTTP_201_CREATED)
