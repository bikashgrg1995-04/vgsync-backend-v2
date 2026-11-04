"""Excel and PDF versions of the reports: each builds a ``Document``."""

from apps.common.export import INT, MONEY, QTY, TEXT, Column, Document, Table

AGEING = [
    ("current", "0-30 days"),
    ("31_60", "31-60 days"),
    ("61_90", "61-90 days"),
    ("over_90", "Over 90 days"),
]


def _period(data):
    start, end = data.get("date_from"), data.get("date_to")

    if start or end:
        return [f"Period: {start or 'the start'} to {end or 'today'}"]

    return ["Period: all dates"]


def _head(business, data):
    return [f"Business: {business.name}"] + _period(data)


def profit_loss(data, business):
    income, expenses = data["income"], data["expenses"]

    return Document(
        title="Profit and loss",
        lines=_head(business, data),
        summary=[
            ("Income", income["total"]),
            ("Refunds to customers", income["sale_refunds"]),
            ("Net income", income["net"]),
            ("Expenses", expenses["total"]),
            ("Refunds from suppliers", expenses["purchase_refunds"]),
            ("Net expenses", expenses["net"]),
            ("Profit", data["profit"]),
        ],
        tables=[
            Table("Income by category", [Column("Category"), Column("Total", MONEY)],
                  [[r["category"], r["total"]] for r in income["by_category"]]),
            Table("Expenses by category", [Column("Category"), Column("Total", MONEY)],
                  [[r["category"], r["total"]] for r in expenses["by_category"]]),
            Table("By month",
                  [Column("Month"), Column("Income", MONEY), Column("Expenses", MONEY),
                   Column("Profit", MONEY)],
                  [[r["month"], r["income"], r["expenses"], r["profit"]] for r in data["by_month"]]),
        ],
        filename="profit-loss",
    )


def _ageing(data, business, title, party, filename):
    summary = [("Total", data["total"])] + [
        (label, data["buckets"][key]) for key, label in AGEING
    ]

    if "emi_outstanding" in data:
        summary.append(("EMI balance (not aged)", data["emi_outstanding"]))

    columns = [Column(party)] + [Column(label, MONEY) for _, label in AGEING] + [Column("Total", MONEY)]

    return Document(
        title=title,
        lines=[f"Business: {business.name}", f"As of: {data['as_of']}"],
        summary=summary,
        tables=[Table(party + "s", columns, [
            [r["name"]] + [r[key] for key, _ in AGEING] + [r["total"]]
            for r in data["parties"]
        ])],
        filename=filename,
    )


def receivables(data, business):
    return _ageing(data, business, "Receivables (customers owe)", "Customer", "receivables")


def payables(data, business):
    return _ageing(data, business, "Payables (owed to suppliers)", "Supplier", "payables")


def stock_value(data, business):
    return Document(
        title="Stock value",
        lines=[f"Business: {business.name}", "Valued at purchase price"],
        summary=[
            ("Good stock", data["good_value"]),
            ("Damaged stock", data["damaged_value"]),
            ("Total", data["total_value"]),
            ("Good units", data["good_units"]),
            ("Damaged units", data["damaged_units"]),
            ("Products without a price", data["products_without_price"]),
        ],
        tables=[Table("Products", [
            Column("Stock no"), Column("Product"), Column("Price", MONEY),
            Column("Good", QTY), Column("Damaged", QTY),
            Column("Good value", MONEY), Column("Damaged value", MONEY),
        ], [
            [r["stock_no"], r["name"], r["purchase_price"], r["quantity"],
             r["damaged_quantity"], r["good_value"], r["damaged_value"]]
            for r in data["products"]
        ])],
        filename="stock-value",
    )


def sales_trend(data, business):
    return Document(
        title=f"Sales by {data['group_by']}",
        lines=_head(business, data),
        summary=[
            ("Sales", data["sales"]), ("Total", data["total"]),
            ("Returns", data["returns"]), ("Net", data["net"]),
        ],
        tables=[Table("Periods", [
            Column("Period"), Column("Sales", INT), Column("Total", MONEY),
            Column("Returns", MONEY), Column("Net", MONEY),
        ], [[r["period"], r["sales"], r["total"], r["returns"], r["net"]] for r in data["periods"]])],
        filename="sales-trend",
    )


def top_products(data, business):
    return Document(
        title=f"Top products by {data['by']}",
        lines=_head(business, data),
        tables=[Table("Products", [
            Column("Stock no"), Column("Product"), Column("Sold", QTY),
            Column("Returned", QTY), Column("Net quantity", QTY), Column("Revenue", MONEY),
        ], [
            [r["stock_no"], r["name"], r["quantity"], r["returned_quantity"],
             r["net_quantity"], r["revenue"]]
            for r in data["products"]
        ])],
        filename="top-products",
    )


def top_customers(data, business):
    return Document(
        title="Top customers",
        lines=_head(business, data),
        summary=[
            ("Walk-in sales", data["walk_in"]["sales"]),
            ("Walk-in total", data["walk_in"]["total"]),
        ],
        tables=[Table("Customers", [
            Column("Customer"), Column("Sales", INT), Column("Total", MONEY), Column("Returns", MONEY),
        ], [[r["name"], r["sales"], r["total"], r["returns"]] for r in data["customers"]])],
        filename="top-customers",
    )


def top_suppliers(data, business):
    return Document(
        title="Top suppliers",
        lines=_head(business, data),
        tables=[Table("Suppliers", [
            Column("Supplier"), Column("Purchases", INT), Column("Total", MONEY),
        ], [[r["name"], r["purchases"], r["total"]] for r in data["suppliers"]])],
        filename="top-suppliers",
    )


def payroll_summary(data, business):
    return Document(
        title="Payroll summary",
        lines=_head(business, data),
        summary=[("Earned", data["earned"]), ("Paid", data["paid"]), ("Owed now", data["owed_now"])],
        tables=[
            Table("Staff", [
                Column("Staff"), Column("Earned", MONEY), Column("Paid", MONEY), Column("Owed now", MONEY),
            ], [[r["name"], r["earned"], r["paid"], r["owed_now"]] for r in data["staff"]]),
            Table("By month", [Column("Month"), Column("Earned", MONEY), Column("Paid", MONEY)],
                  [[r["month"], r["earned"], r["paid"]] for r in data["by_month"]]),
        ],
        filename="payroll-summary",
    )


def statement(data, business, kind):
    """A customer or supplier statement. ``kind`` is ``customer`` or ``supplier``."""
    party = data["party"]["name"]

    return Document(
        title=f"Statement: {party}",
        lines=_head(business, data) + [data["balance_means"]],
        summary=[
            ("Opening balance", data["opening_balance"]),
            ("Total debit", data["total_debit"]),
            ("Total credit", data["total_credit"]),
            ("Closing balance", data["closing_balance"]),
        ],
        tables=[Table("Transactions", [
            Column("Date"), Column("Type"), Column("Reference"), Column("Description"),
            Column("Debit", MONEY), Column("Credit", MONEY), Column("Balance", MONEY),
        ], [
            [str(r["date"]), r["type"], r["reference"], r["description"],
             r["debit"], r["credit"], r["balance"]]
            for r in data["results"]
        ])],
        filename=f"{kind}-statement-{data['party']['id']}",
    )
