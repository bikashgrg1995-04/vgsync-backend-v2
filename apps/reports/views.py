from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.dates import parse_query_date
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated

from apps.accounts import rbac
from apps.common.permissions import HasActiveBusinessContext, HasPermission

from apps.common import export

from . import exports, queries


def _date(request, name):
    raw = request.query_params.get(name)

    if not raw:
        return None

    value = parse_query_date(raw)

    if value is None:
        raise ValidationError({name: "Use a real date as YYYY-MM-DD."})

    return value


def _range(request):
    date_from = _date(request, "date_from")
    date_to = _date(request, "date_to")

    if date_from and date_to and date_from > date_to:
        raise ValidationError({"date_to": "This is before date_from."})

    return date_from, date_to


def _limit(request, default=10):
    raw = request.query_params.get("limit")

    if not raw:
        return default

    try:
        value = int(raw)
    except ValueError:
        value = 0

    if not 1 <= value <= 100:
        raise ValidationError({"limit": "Use a whole number from 1 to 100."})

    return value


def _choice(request, name, allowed, default):
    value = request.query_params.get(name) or default

    if value not in allowed:
        raise ValidationError({name: f"Use one of: {', '.join(allowed)}."})

    return value


class OwnerReport(APIView):
    """
    Reports belong to the owner of the active business. Add
    ``?export=xlsx`` or ``?export=pdf`` to download one as a file.
    """

    permission_classes = [
        IsAuthenticated,
        HasActiveBusinessContext,
        HasPermission,
    ]
    required_permission = "reports.view"
    builder = None

    def output(self, request, data):
        kind = export.requested(request)

        if kind is None:
            return Response(data)

        if not rbac.has_permission(
            request.user, "reports.export", request.access_mode
        ):
            raise PermissionDenied("You may not export reports.")

        document = type(self).builder(data, request.active_business)

        return export.respond(document, kind)


class ProfitLossView(OwnerReport):
    """
    Profit and loss of the active business on a cash basis: income less
    refunds to customers, against expenses less refunds from suppliers.
    Filters: ``date_from``, ``date_to``. Includes a month by month split.
    """

    builder = staticmethod(exports.profit_loss)

    def get(self, request):
        date_from = _date(request, "date_from")
        date_to = _date(request, "date_to")

        if date_from and date_to and date_from > date_to:
            raise ValidationError({"date_to": "This is before date_from."})

        report = queries.profit_and_loss(request.active_business, date_from, date_to)

        return self.output(request, {
            "date_from": date_from,
            "date_to": date_to,
            **report,
        })


class ReceivablesView(OwnerReport):
    """What customers owe, aged by sale date. EMI sales are shown apart."""

    builder = staticmethod(exports.receivables)

    def get(self, request):
        today = timezone.localdate()

        return self.output(request, {
            "as_of": today,
            **queries.receivables(request.active_business, today),
        })


class PayablesView(OwnerReport):
    """What is owed to suppliers on received goods, aged by purchase date."""

    builder = staticmethod(exports.payables)

    def get(self, request):
        today = timezone.localdate()

        return self.output(request, {
            "as_of": today,
            **queries.payables(request.active_business, today),
        })


class StockValueView(OwnerReport):
    """Stock on hand valued at purchase price, good and damaged apart."""

    builder = staticmethod(exports.stock_value)

    def get(self, request):
        return self.output(request, queries.stock_value(request.active_business))


class SalesTrendView(OwnerReport):
    """
    Confirmed sales per ``group_by`` (``day``, ``week`` or ``month``, default
    ``month``), less returns. Filters: ``date_from``, ``date_to``.
    """

    builder = staticmethod(exports.sales_trend)

    def get(self, request):
        date_from, date_to = _range(request)
        group_by = _choice(request, "group_by", ("day", "week", "month"), "month")

        return self.output(request, {
            "date_from": date_from,
            "date_to": date_to,
            **queries.sales_trend(request.active_business, date_from, date_to, group_by),
        })


class TopProductsView(OwnerReport):
    """
    Best sellers. ``by`` is ``value`` (revenue, the default) or ``quantity``;
    ``limit`` is 1 to 100 (default 10). Filters: ``date_from``, ``date_to``.
    """

    builder = staticmethod(exports.top_products)

    def get(self, request):
        date_from, date_to = _range(request)

        return self.output(request, {
            "date_from": date_from,
            "date_to": date_to,
            **queries.top_products(
                request.active_business, date_from, date_to,
                _limit(request), _choice(request, "by", ("value", "quantity"), "value"),
            ),
        })


class TopCustomersView(OwnerReport):
    """Customers by what they bought; walk-in sales are totalled apart."""

    builder = staticmethod(exports.top_customers)

    def get(self, request):
        date_from, date_to = _range(request)

        return self.output(request, {
            "date_from": date_from,
            "date_to": date_to,
            **queries.top_customers(
                request.active_business, date_from, date_to, _limit(request)
            ),
        })


class TopSuppliersView(OwnerReport):
    """Suppliers by what was bought from them."""

    builder = staticmethod(exports.top_suppliers)

    def get(self, request):
        date_from, date_to = _range(request)

        return self.output(request, {
            "date_from": date_from,
            "date_to": date_to,
            **queries.top_suppliers(
                request.active_business, date_from, date_to, _limit(request)
            ),
        })


class PayrollSummaryView(OwnerReport):
    """What staff earned and were paid in the period, and what is owed now."""

    builder = staticmethod(exports.payroll_summary)

    def get(self, request):
        date_from, date_to = _range(request)

        return self.output(request, {
            "date_from": date_from,
            "date_to": date_to,
            **queries.payroll_summary(request.active_business, date_from, date_to),
        })


class DashboardView(APIView):
    """
    The owner dashboard in one call. Head Office owners also get a branch
    overview (read-only). Needs ``dashboard.view``.
    """

    permission_classes = [
        IsAuthenticated,
        HasActiveBusinessContext,
        HasPermission,
    ]
    required_permission = "dashboard.view"

    def get(self, request):
        from . import dashboard

        return Response(
            dashboard.owner_dashboard(
                request.active_business,
                request.user,
                request.access_mode,
                timezone.localdate(),
            )
        )
