"""
The owner dashboard: one call, real data from the same queries the reports
use. Head Office also gets a read-only overview of its branches.
"""

import datetime
from decimal import Decimal

from django.db.models import F, Sum

from apps.accounts.models import User
from apps.approvals.models import ApprovalRequest
from apps.businesses.models import Business
from apps.finance.models import Expense, Income
from apps.inventory.models import StockBalance
from apps.purchases.models import Purchase
from apps.sales.models import Sale

from . import queries
from .queries import ZERO, money


def _sum(queryset, field):
    return queryset.aggregate(t=Sum(field))["t"] or ZERO


def _cash_position(business):
    """Money in less money out, by how it was paid. Active entries only."""
    methods = {}

    for model, sign in ((Income, 1), (Expense, -1)):
        rows = (
            model.objects.filter(business=business, status="active")
            .values("payment_method")
            .annotate(total=Sum("amount"))
        )

        for row in rows:
            key = row["payment_method"] or "other"
            methods[key] = methods.get(key, ZERO) + sign * row["total"]

    cash = methods.pop("cash", ZERO)
    bank = methods.pop("bank", ZERO) + methods.pop("online", ZERO)
    other = sum(methods.values(), ZERO)

    return {
        "cash": money(cash),
        "bank": money(bank),
        "other": money(other),
        "total": money(cash + bank + other),
    }


def _low_stock(business, limit=8):
    rows = (
        StockBalance.objects.filter(
            business=business,
            product__reorder_level__gt=0,
            quantity__lte=F("product__reorder_level"),
        )
        .select_related("product")
        .order_by("quantity")
    )

    return {
        "count": rows.count(),
        "items": [
            {
                "product": row.product_id,
                "name": row.product.name,
                "quantity": f"{row.quantity:.3f}",
                "reorder_level": f"{row.product.reorder_level:.3f}",
            }
            for row in rows[:limit]
        ],
    }


def _staff(business):
    from apps.accounts.scope import scoped_users

    users = scoped_users(business)

    return {
        "total": users.count(),
        "active": users.filter(is_active=True).count(),
        "without_position": users.filter(
            is_active=True, position__isnull=True
        ).count(),
    }


def _snapshot(business, today):
    month_start = today.replace(day=1)
    sales = Sale.objects.filter(business=business, status=Sale.Status.CONFIRMED)
    purchases = Purchase.objects.filter(
        business=business, status__in=Purchase.BILLED
    )

    return {
        "sales_today": money(_sum(sales.filter(sale_date=today), "net_total")),
        "sales_month": money(
            _sum(sales.filter(sale_date__gte=month_start), "net_total")
        ),
        "purchases_month": money(
            _sum(purchases.filter(purchase_date__gte=month_start), "net_total")
        ),
        "profit_month": queries.profit_and_loss(business, month_start, today)[
            "profit"
        ],
    }


def _branch_overview(company_id, today):
    branches = Business.objects.filter(
        company_id=company_id,
        business_type=Business.BusinessType.BRANCH,
        is_active=True,
    ).order_by("name")

    return [
        {
            "id": branch.pk,
            "name": branch.name,
            "business_code": branch.business_code,
            **_snapshot(branch, today),
            "receivables": queries.receivables(branch, today)["total"],
            "low_stock": _low_stock(branch, limit=0)["count"],
        }
        for branch in branches
    ]


def owner_dashboard(business, user, access_mode, today=None):
    today = today or datetime.date.today()
    month_start = today.replace(day=1)
    since = today - datetime.timedelta(days=29)

    receivables = queries.receivables(business, today)
    payables = queries.payables(business, today)
    stock = queries.stock_value(business)
    pnl = queries.profit_and_loss(business, month_start, today)
    trend = queries.sales_trend(business, since, today, "day")
    low = _low_stock(business)
    pending = ApprovalRequest.objects.filter(
        business=business, status=ApprovalRequest.Status.PENDING
    ).count()

    alerts = []

    if low["count"]:
        alerts.append({
            "kind": "low_stock", "severity": "warning",
            "message": f"{low['count']} products are at or below reorder level.",
        })

    if Decimal(receivables["buckets"]["over_90"]) > 0:
        alerts.append({
            "kind": "overdue_receivables", "severity": "danger",
            "message": "Customers owe money older than 90 days.",
        })

    if pending:
        alerts.append({
            "kind": "approvals", "severity": "info",
            "message": f"{pending} requests are waiting for approval.",
        })

    data = {
        "as_of": today,
        "business": {
            "id": business.pk,
            "name": business.name,
            "business_type": business.business_type,
        },
        "access_mode": access_mode,
        **_snapshot(business, today),
        "income_month": pnl["income"]["net"],
        "expenses_month": pnl["expenses"]["net"],
        "receivables": receivables["total"],
        "payables": payables["total"],
        "cash": _cash_position(business),
        "stock": {
            "value": stock["total_value"],
            "damaged_value": stock["damaged_value"],
        },
        "sales_trend": [
            {"date": row["period"], "total": row["net"]}
            for row in trend["periods"]
        ],
        "income_expense_by_month": pnl["by_month"],
        "top_products": queries.top_products(
            business, month_start, today, 5, "value"
        )["products"],
        "low_stock": low,
        "approvals_pending": pending,
        "staff": _staff(business),
        "alerts": alerts,
    }

    # Head Office sees its branches, never edits them from here.
    if (
        user.role == User.Role.OWNER
        and business.business_type == Business.BusinessType.HEAD_OFFICE
    ):
        data["branches"] = _branch_overview(business.company_id, today)

    return data
