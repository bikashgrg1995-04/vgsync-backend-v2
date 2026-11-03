from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.dates import parse_query_date
from apps.common.permissions import IsOwner

from . import queries


def _date(request, name):
    raw = request.query_params.get(name)

    if not raw:
        return None

    value = parse_query_date(raw)

    if value is None:
        raise ValidationError({name: "Use a real date as YYYY-MM-DD."})

    return value


class OwnerReport(APIView):
    """Reports belong to the owner of the active business."""

    permission_classes = [IsOwner]


class ProfitLossView(OwnerReport):
    """
    Profit and loss of the active business on a cash basis: income less
    refunds to customers, against expenses less refunds from suppliers.
    Filters: ``date_from``, ``date_to``. Includes a month by month split.
    """

    def get(self, request):
        date_from = _date(request, "date_from")
        date_to = _date(request, "date_to")

        if date_from and date_to and date_from > date_to:
            raise ValidationError({"date_to": "This is before date_from."})

        report = queries.profit_and_loss(request.active_business, date_from, date_to)

        return Response({
            "date_from": date_from,
            "date_to": date_to,
            **report,
        })


class ReceivablesView(OwnerReport):
    """What customers owe, aged by sale date. EMI sales are shown apart."""

    def get(self, request):
        today = timezone.localdate()

        return Response({
            "as_of": today,
            **queries.receivables(request.active_business, today),
        })


class PayablesView(OwnerReport):
    """What is owed to suppliers on received goods, aged by purchase date."""

    def get(self, request):
        today = timezone.localdate()

        return Response({
            "as_of": today,
            **queries.payables(request.active_business, today),
        })


class StockValueView(OwnerReport):
    """Stock on hand valued at purchase price, good and damaged apart."""

    def get(self, request):
        return Response(queries.stock_value(request.active_business))
