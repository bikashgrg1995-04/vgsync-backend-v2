"""
Bring the history of the old VGSync system (a Django ``dumpdata`` JSON file)
into one business.

What comes in: products and their current stock, suppliers, purchases with
their lines and payments, customers (made from the sales), sales with lines,
payments and follow-ups, and expenses.

What does not: the old logins, staff and salary sheets, and customer orders.
They are counted in the report so nothing is silently dropped.

History is written straight to the tables. It never moves stock (the stock
quantity in the file already is the result of all that history) and sends no
notifications. The import is all or nothing, and the caller takes a safety
copy first.
"""

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

NEPAL = ZoneInfo("Asia/Kathmandu")
MAX_BYTES = 40 * 1024 * 1024
CENT = Decimal("0.01")

SKIPPED_MODELS = {
    "core.user": "old logins (create the users again in Staff)",
    "core.staff": "staff records (add them in Staff)",
    "core.salarytracker": "salary sheets",
    "core.salarytransaction": "salary payments (the matching expenses are kept)",
    "core.order": "customer orders",
    "core.category": None,  # used for product categories, not skipped
}


class LegacyError(Exception):
    pass


def money(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def qty(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def local_date(value):
    """A date in Nepal for a timestamp (or date) from the old system."""
    if not value:
        return None

    text = str(value)

    if "T" not in text:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()

    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo("UTC"))

    return moment.astimezone(NEPAL).date()


def read(data):
    if len(data) > MAX_BYTES:
        raise LegacyError("This file is too large.")

    try:
        rows = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise LegacyError("This is not a readable JSON file.")

    if not isinstance(rows, list) or not all(
        isinstance(r, dict) and "model" in r and "fields" in r for r in rows
    ):
        raise LegacyError("This is not a Django dumpdata file (a list of model/fields).")

    grouped = defaultdict(list)

    for row in rows:
        grouped[row["model"]].append(row)

    if not grouped.get("core.stock") and not grouped.get("core.sale"):
        raise LegacyError("This file has no old stock or sales to import.")

    return grouped


def summary(grouped):
    """What the file holds, and what will not be imported."""
    counts = {m.split(".")[1]: len(r) for m, r in grouped.items()}
    skipped = [
        {"what": why, "count": len(grouped[m])}
        for m, why in SKIPPED_MODELS.items()
        if why and grouped.get(m)
    ]

    return {"counts": counts, "not_imported": skipped}


def _clip(text, size):
    return str(text or "").strip()[:size]


def _phone_key(value):
    return re.sub(r"\s+", "", str(value or "")).strip()[:30]


def _product_rows(grouped):
    """Products by old pk; a stock number used twice becomes one product."""
    categories = {c["pk"]: c["fields"]["name"] for c in grouped.get("core.category", [])}
    by_no, pk_to_no, warnings = {}, {}, []

    for row in grouped.get("core.stock", []):
        f = row["fields"]
        number = _clip(f["item_no"], 100)
        key = number.lower()

        if not number:
            warnings.append(f"A stock row (id {row['pk']}) has no item number and was skipped.")
            continue

        name_now = " ".join(str(f["name"] or "").lower().split())

        if key in by_no and by_no[key]["_name"] != name_now:
            # Same stock number, different item: keep both, number the later one.
            n = 2

            while f"{key} ({n})" in by_no:
                n += 1

            number, key = f"{number} ({n})"[:100], f"{key} ({n})"
            warnings.append(
                f"Stock No {f['item_no']} is used by two different items ({f['name']}); "
                f"the second one was saved as {number}."
            )

        if key in by_no:
            warnings.append(
                f"Stock No {number} appears more than once with the same name in the old data; the entries were merged."
            )
            by_no[key]["quantity"] += qty(f["stock"])
        else:
            block = _clip(f.get("block"), 40)
            by_no[key] = {
                "_name": name_now,
                "stock_no": number,
                "name": _clip(f["name"], 200) or number,
                "model": _clip(f.get("model"), 100),
                "category": _clip(categories.get(f.get("category")), 100),
                "group": _clip(f.get("group"), 100),
                "description": f"Rack/block: {block}" if block else "",
                "purchase_price": money(f["purchase_price"]),
                "sale_price": money(f["sale_price"]),
                "quantity": qty(f["stock"]),
            }

        pk_to_no[row["pk"]] = key

    return by_no, pk_to_no, warnings


def run(user, business, data, *, apply=False):
    """
    Check (and with ``apply`` save) the old data. Returns what was or would
    be created, and warnings. Raises ``LegacyError`` for a file or business
    that cannot take it.
    """
    from apps.businesses.models import Business

    grouped = read(data)
    result = {**summary(grouped), "warnings": [], "created": {}}

    if business.business_type == Business.BusinessType.BRANCH:
        raise LegacyError("Import into the Head Office or an independent business, not a branch.")

    from apps.purchases.models import Purchase
    from apps.sales.models import Sale

    if Sale.objects.filter(business=business).exists() or Purchase.objects.filter(
        business=business
    ).exists():
        raise LegacyError(
            "This business already has sales or purchases. Old data can only be imported "
            "into a business that has none, so nothing is entered twice."
        )

    products, pk_to_no, warnings = _product_rows(grouped)
    result["warnings"].extend(warnings)

    if not apply:
        result["created"] = {
            "products": len(products),
            "products_with_stock": sum(1 for p in products.values() if p["quantity"] > 0),
            "suppliers": len(grouped.get("core.supplier", [])),
            "purchases": len(grouped.get("core.purchase", [])),
            "sales": len(grouped.get("core.sale", [])),
            "followups": len(grouped.get("core.followupdashboard", [])),
            "expenses": len(grouped.get("core.expense", [])),
        }

        return result

    with transaction.atomic():
        result["created"] = _write(user, business, grouped, products, pk_to_no, result["warnings"])

    return result


def _write(user, business, grouped, products, pk_to_no, warnings):
    from apps.finance.models import Expense, Income
    from apps.inventory.models import Product, StockMovement
    from apps.inventory.services.stock import create_stock_movement
    from apps.purchases.models import (
        Purchase, PurchaseItem, PurchasePayment, Supplier,
    )
    from apps.sales.models import Customer, Followup, Sale, SaleItem, SalePayment

    made = {}

    # -- products and stock ----------------------------------------------
    existing = {p.stock_no.lower(): p for p in Product.objects.filter(owner_business=business)}
    new = [
        Product(
            owner_business=business, stock_no=p["stock_no"], name=p["name"], model=p["model"],
            category=p["category"], group=p["group"], description=p["description"],
            purchase_price=p["purchase_price"], sale_price=p["sale_price"], mrp=p["sale_price"],
        )
        for key, p in products.items() if key not in existing
    ]
    Product.objects.bulk_create(new, batch_size=1000)
    product = {p.stock_no.lower(): p for p in Product.objects.filter(owner_business=business)}
    made["products"] = len(new)

    if len(new) < len(products):
        warnings.append(
            f"{len(products) - len(new)} products already existed here; their details and stock were left as they are."
        )

    stocked = 0

    for key, row in products.items():
        if key in existing or row["quantity"] <= 0:
            continue

        create_stock_movement(
            user=user, business=business, product=product[key],
            movement_type=StockMovement.MovementType.OPENING, quantity=row["quantity"],
            reference="Old system", notes="Opening stock from the old system",
        )
        stocked += 1

    made["products_with_stock"] = stocked
    by_old = {pk: product[key] for pk, key in pk_to_no.items() if key in product}

    # -- suppliers ---------------------------------------------------------
    suppliers = {}

    for row in grouped.get("core.supplier", []):
        f = row["fields"]
        name = _clip(f["name"], 200) or f"Supplier {row['pk']}"
        suppliers[row["pk"]], _ = Supplier.objects.get_or_create(
            business=business, name=name,
            defaults={"phone": _clip(f.get("contact"), 30), "address": _clip(f.get("address"), 255),
                      "email": _clip(f.get("email"), 254)},
        )

    made["suppliers"] = len(suppliers)

    # -- purchases ---------------------------------------------------------
    lines = defaultdict(list)

    for row in grouped.get("core.purchaseitem", []):
        lines[row["fields"]["purchase"]].append(row["fields"])

    used_bills, count, number = set(), 0, 0
    purchases = sorted(grouped.get("core.purchase", []), key=lambda r: (r["fields"]["date"], r["pk"]))

    for row in purchases:
        f = row["fields"]
        supplier = suppliers.get(f["supplier"])

        if supplier is None:
            warnings.append(f"Purchase {row['pk']} has no supplier and was skipped.")
            continue

        if (
            money(f["grand_total"]) < 0 or money(f["net_total"]) < 0
            or any(qty(i["quantity"]) <= 0 for i in lines.get(row["pk"], []))
        ):
            warnings.append(
                f"Purchase {row['pk']} is a negative (return-style) entry that a purchase "
                "cannot hold, so it was not imported; the stock quantity already includes it."
            )
            continue

        number += 1
        bill = _clip(f.get("bill_no"), 60)

        if bill and (supplier.pk, bill.lower()) in used_bills:
            warnings.append(f"Bill {bill} was used on more than one purchase; the repeat got a suffix.")
            bill = f"{bill[:50]}-P{row['pk']}"

        used_bills.add((supplier.pk, bill.lower()))
        date = local_date(f["date"])
        subtotal = money(f["grand_total"])
        discount = min(money(f["discount_amount"]) + money(f.get("transit_discount_amount")), subtotal)
        net = money(f["net_total"])

        purchase = Purchase.objects.create(
            business=business, number=number, supplier=supplier, bill_no=bill,
            purchase_date=date, status=Purchase.Status.RECEIVED, subtotal=subtotal,
            discount_amount=discount, vat_rate=min(Decimal(str(f["vat_percentage"] or 0)), Decimal(100)),
            vat_amount=money(f["vat_amount"]), net_total=net, created_by=user,
            received_by=user, received_at=timezone.now(),
            notes=f"Imported from the old system (purchase {row['pk']}).",
        )
        count += 1

        for item in lines.get(row["pk"], []):
            prod = by_old.get(item["item"])

            if prod is None:
                warnings.append(f"Purchase {row['pk']}: a line used an unknown product and was skipped.")
                continue

            quantity, price = qty(item["quantity"]), max(money(item["price"]), CENT)
            PurchaseItem.objects.create(
                purchase=purchase, product=prod, quantity=quantity, received_quantity=quantity,
                unit_price=price, line_total=max((quantity * price).quantize(CENT), CENT),
            )

        paid = money(f["paid_amount"])

        if paid > 0:
            method = "online" if f.get("paid_from") == "online" else "cash"
            payment = PurchasePayment.objects.create(
                purchase=purchase, amount=paid, paid_on=date, method=method,
                notes="Imported from the old system", paid_by=user,
            )
            Expense.objects.create(
                business=business, category=Expense.Category.PURCHASE, amount=paid,
                expense_date=date, description=f"Purchase {purchase.purchase_no}",
                payment_method=method, purchase_payment=payment, created_by=user,
            )

    made["purchases"] = count

    # -- customers ---------------------------------------------------------
    customers, sales_old = {}, grouped.get("core.sale", [])

    def customer_for(f):
        name = _clip(f["customer_name"], 200)
        phone = _phone_key(f.get("contact_no"))

        if not name and not phone:
            return None

        key = ("p", phone) if phone else ("n", name.lower())

        if key not in customers:
            customers[key] = Customer.objects.create(
                business=business, name=name or phone, phone=phone,
            )

        return customers[key]

    # -- sales ---------------------------------------------------------------
    sale_lines = defaultdict(list)

    for row in grouped.get("core.saleitem", []):
        sale_lines[row["fields"]["sale"]].append(row["fields"])

    sale_obj, count, number = {}, 0, 0

    for row in sorted(sales_old, key=lambda r: (r["fields"]["sale_date"], r["pk"])):
        f = row["fields"]
        number += 1
        date = local_date(f["sale_date"])
        subtotal = money(f["grand_total"])
        discount = min(money(f["discount_amount"]), subtotal)
        net = money(f["net_total"])
        maintenance = bool(f["is_servicing"])
        notes = " | ".join(
            part for part in (
                f"Imported from the old system ({f['sale_ref']})",
                _clip(f.get("remarks"), 200),
                f"Technician: {_clip(f.get('technician_name'), 80)}" if f.get("technician_name") else "",
                f"Bill no: {_clip(f.get('bill_no'), 40)}" if f.get("bill_no") else "",
                f"Job card: {_clip(f.get('job_card_no'), 40)}" if f.get("job_card_no") else "",
                "Fitting" if f.get("is_fitting") else "",
            ) if part
        )

        sale = Sale.objects.create(
            business=business, number=number, invoice_number=number,
            customer=customer_for(f), sale_date=date,
            sale_type=Sale.SaleType.MAINTENANCE if maintenance else Sale.SaleType.STOCK,
            status=Sale.Status.CONFIRMED, payment_mode=Sale.PaymentMode.FULL,
            service_kind=Sale.ServiceKind.ACCIDENT if f.get("is_accident") else Sale.ServiceKind.PAID,
            odometer_km=int(f["km_driven"]) if str(f.get("km_driven") or "").isdigit() else None,
            job_status=Sale.JobStatus.DONE if maintenance else Sale.JobStatus.PENDING,
            job_notes=_clip(f.get("job_done_on_vehicle"), 2000) if maintenance else "",
            vehicle_reg_no=_clip(f.get("bike_registration_no"), 30) if maintenance else "",
            subtotal=subtotal, discount_amount=discount, net_total=net,
            notes=notes, created_by=user, confirmed_by=user, confirmed_at=timezone.now(),
        )
        sale_obj[row["pk"]] = sale
        count += 1
        total_lines = Decimal("0")

        for item in sale_lines.get(row["pk"], []):
            prod = by_old.get(item["item"])

            if prod is None:
                warnings.append(f"Sale {f['sale_ref']}: a line used an unknown product and was skipped.")
                continue

            quantity, price = qty(item["quantity"]), money(item["sale_price"])
            line = money(item["total_price"])
            SaleItem.objects.create(
                sale=sale, product=prod, quantity=quantity, unit_price=price, line_total=line,
            )
            total_lines += line

        extra = subtotal - total_lines

        if extra > 0:
            SaleItem.objects.create(
                sale=sale, description="Labour / other charges", quantity=1,
                unit_price=extra, line_total=extra,
            )

        paid = money(f["paid_amount"])

        if paid > 0:
            method = "online" if f.get("paid_from") == "online" else "cash"
            payment = SalePayment.objects.create(
                sale=sale, amount=paid, paid_on=date, method=method,
                notes="Imported from the old system", received_by=user,
            )
            Income.objects.create(
                business=business,
                category=Income.Category.MAINTENANCE if maintenance else Income.Category.STOCK_SALE,
                amount=paid, income_date=date, description=f"Sale {sale.sale_no}",
                payment_method=method, sale_payment=payment, created_by=user,
            )

        remaining = net - paid

        if remaining > 0:
            Followup.objects.create(
                business=business, sale=sale, customer=sale.customer,
                kind=Followup.Kind.CREDIT_DUE, due_date=date, amount=remaining,
                note="Unpaid in the old system",
            )

    made["sales"] = count

    # -- follow-ups from the old follow-up dashboard ----------------------------
    followups = 0

    for row in grouped.get("core.followupdashboard", []):
        f = row["fields"]
        sale = sale_obj.get(f["sale"])
        due = local_date(f.get("follow_up_date"))

        if sale is None or due is None:
            continue

        status = Followup.Status.SKIPPED if f.get("status") == "terminated" else Followup.Status.PENDING
        Followup.objects.create(
            business=business, sale=sale, customer=sale.customer,
            kind=Followup.Kind.MAINTENANCE_DUE, due_date=due, status=status,
            completed_on=local_date(f.get("terminated_at")),
            note=_clip(" - ".join(p for p in (f.get("reason"), f.get("remarks")) if p), 300),
        )
        followups += 1

    made["followups"] = followups

    # -- expenses (purchase payments were made above) -----------------------------
    categories = {"salary": Expense.Category.SALARY}
    expenses = 0

    for row in grouped.get("core.expense", []):
        f = row["fields"]

        if f.get("reference_type") == "Purchase":
            continue

        amount = money(f["amount"])

        if amount <= 0:
            continue

        method = f.get("payment_method")
        Expense.objects.create(
            business=business, category=categories.get(f["expense_type"], Expense.Category.OTHER),
            amount=amount, expense_date=local_date(f["expense_date"]),
            description=_clip(f["title"], 200),
            payment_method=method if method in ("cash", "bank", "online", "cheque") else "cash",
            reference=_clip(f.get("note"), 100), created_by=user,
        )
        expenses += 1

    made["expenses"] = expenses

    return made
