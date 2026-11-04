"""
Customers and suppliers as their own section: the list, the profile (with
what is owed each way) and a statement of every transaction with them.
"""

from django.utils.dateparse import parse_date
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.common import export
from apps.common.pagination import StandardPagination
from apps.purchases.views import SupplierViewSet as BaseSupplierViewSet
from apps.sales.views import CustomerViewSet as BaseCustomerViewSet

from apps.reports import exports as report_exports

from . import ledger


class StatementPagination(StandardPagination):
    page_size = 50
    max_page_size = 200


def _date(request, name):
    raw = request.query_params.get(name)

    if not raw:
        return None

    try:
        value = parse_date(raw)
    except ValueError:
        # Right shape, impossible date (for example month 13).
        value = None

    if value is None:
        raise ValidationError({name: "Use the format YYYY-MM-DD."})

    return value


def _types(request, allowed):
    chosen = []

    for raw in request.query_params.getlist("type"):
        chosen.extend(part.strip() for part in raw.split(",") if part.strip())

    unknown = [t for t in chosen if t not in allowed]

    if unknown:
        raise ValidationError({
            "type": f"Unknown type {unknown[0]}. Choose from: {', '.join(allowed)}."
        })

    return chosen


def statement_response(view, request, party, rows, allowed, balance_means, kind):
    file_type = export.requested(request)
    date_from = _date(request, "date_from")
    date_to = _date(request, "date_to")

    if date_from and date_to and date_from > date_to:
        raise ValidationError({"date_from": "This is after date_to."})

    result = ledger.build_statement(
        rows, date_from, date_to, _types(request, allowed)
    )

    if file_type:
        # A download carries every row, not one page of them.
        data = {
            "party": {"id": party.pk, "name": party.name},
            "date_from": date_from,
            "date_to": date_to,
            "balance_means": balance_means,
            "opening_balance": ledger.money_string(result["opening_balance"]),
            "total_debit": ledger.money_string(result["total_debit"]),
            "total_credit": ledger.money_string(result["total_credit"]),
            "closing_balance": ledger.money_string(result["closing_balance"]),
            "results": [ledger.render_row(row) for row in result["rows"]],
        }
        document = report_exports.statement(data, request.active_business, kind)

        return export.respond(document, file_type)

    paginator = StatementPagination()
    page = paginator.paginate_queryset(
        [ledger.render_row(row) for row in result["rows"]], request, view=view
    )
    body = paginator.get_paginated_response(page).data

    return Response({
        "party": {"id": party.pk, "name": party.name},
        "date_from": date_from,
        "date_to": date_to,
        "balance_means": balance_means,
        "opening_balance": ledger.money_string(result["opening_balance"]),
        "total_debit": ledger.money_string(result["total_debit"]),
        "total_credit": ledger.money_string(result["total_credit"]),
        "closing_balance": ledger.money_string(result["closing_balance"]),
        **body,
    })


class CustomerViewSet(BaseCustomerViewSet):
    """
    Customers of the active business. The profile shows what the customer
    owes (``credit_to_receive``), what is owed back to them
    (``credit_to_pay``), advances and replacement goods still to give.
    ``statement`` lists every transaction with them.
    """

    @action(detail=True, methods=["get"])
    def statement(self, request, pk=None):
        """
        Every sale, payment, return, refund, replacement and credit
        adjustment with this customer, in date order with a running balance.
        A positive balance is money the customer still has to pay; a
        negative one is credit owed back to them. Filters: ``date_from``,
        ``date_to`` (the opening balance carries what came before),
        ``type`` (one or more of ``sale``, ``payment``, ``return``,
        ``refund``, ``replacement``, ``adjustment``). Add ``export=xlsx`` or
        ``export=pdf`` to download every row as a file.
        """
        customer = self.get_object()

        return statement_response(
            self, request, customer, ledger.customer_rows(customer),
            ledger.CUSTOMER_TYPES, "positive: the customer owes you; negative: you owe the customer",
            "customer",
        )


class SupplierViewSet(BaseSupplierViewSet):
    """
    Suppliers of the active business. The profile shows what is owed to the
    supplier (``credit_to_pay``), what the supplier owes back
    (``credit_to_receive``), advances paid and replacement goods still to
    receive. ``statement`` lists every transaction with them.
    """

    @action(detail=True, methods=["get"])
    def statement(self, request, pk=None):
        """
        Every purchase, payment, return, refund, replacement and credit
        adjustment with this supplier, in date order with a running
        balance. A positive balance is money you still have to pay; a
        negative one is credit the supplier owes you. Filters:
        ``date_from``, ``date_to``, ``type`` (one or more of ``purchase``,
        ``payment``, ``return``, ``refund``, ``replacement``,
        ``adjustment``).
        """
        supplier = self.get_object()

        return statement_response(
            self, request, supplier, ledger.supplier_rows(supplier),
            ledger.SUPPLIER_TYPES, "positive: you owe the supplier; negative: the supplier owes you",
            "supplier",
        )
