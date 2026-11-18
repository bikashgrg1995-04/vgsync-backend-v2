"""
Bring a product list in from Excel or CSV, and take one out again.

One file layout does three jobs, so a sheet can go out, be edited, and come
back: the export, the import template, and the bulk MRP update. Header names
are matched loosely (``Stock No``, ``stock_no`` and ``SKU`` are the same).

An import is all or nothing: a preview lists every problem by row number, and
nothing is saved unless the whole file is clean.
"""

import csv
import io
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework.exceptions import ValidationError

from apps.inventory.models import Product, StockMovement
from apps.inventory.services.products import (
    get_products_for_business,
    validate_product_write_access,
)
from apps.inventory.services.stock import create_stock_movement

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000

# Column key -> names accepted in a header (compared in lower case, with
# spaces, dots and dashes treated as underscores).
ALIASES = {
    "stock_no": ("stock_no", "stock_number", "sku", "code", "item_code", "part_no", "part_number"),
    "name": ("name", "stock_name", "product", "product_name", "item", "item_name"),
    "brand": ("brand", "make"),
    "model": ("model",),
    "category": ("category",),
    "group": ("group",),
    "unit": ("unit", "uom"),
    "mrp": ("mrp", "m_r_p", "max_retail_price"),
    "purchase_price": ("purchase_price", "cost", "cost_price", "buying_price", "purchase_rate", "unit_price", "unit_cost"),
    "sale_price": ("sale_price", "selling_price", "price", "sale_rate", "rate"),
    "reorder_level": ("reorder_level", "reorder", "min_stock", "minimum_stock"),
    "quantity": ("quantity", "qty", "opening_qty", "opening_stock", "stock", "opening_quantity"),
}
TEXT = ("name", "brand", "model", "category", "group", "unit")
MONEY = ("mrp", "purchase_price", "sale_price")

# What an export / template contains, in this order.
EXPORT_COLUMNS = [
    ("stock_no", "Stock No"), ("name", "Name"), ("brand", "Brand"), ("model", "Model"),
    ("category", "Category"), ("group", "Group"), ("unit", "Unit"), ("mrp", "MRP"),
    ("purchase_price", "Purchase Price"), ("sale_price", "Sale Price"),
    ("reorder_level", "Reorder Level"), ("quantity", "Quantity"),
]


def _key(header):
    text = str(header or "").strip().lower()

    for ch in " .-/":
        text = text.replace(ch, "_")

    text = "_".join(p for p in text.split("_") if p)

    for key, names in ALIASES.items():
        if text in names:
            return key

    return None


def read_rows(upload):
    """``(columns, rows)`` from an uploaded .xlsx or .csv; row numbers start at 2."""
    if upload is None:
        raise ValidationError({"file": "Choose the file to import."})

    if upload.size > MAX_BYTES:
        raise ValidationError({"file": "This file is too large (5 MB at most)."})

    name = upload.name.lower()
    data = upload.read()

    if name.endswith(".xlsx"):
        from openpyxl import load_workbook

        try:
            sheet = load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
            table = [list(r) for r in sheet.iter_rows(values_only=True)]
        except Exception:
            raise ValidationError({"file": "This is not a readable Excel (.xlsx) file."})
    elif name.endswith(".csv"):
        try:
            table = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
        except UnicodeDecodeError:
            raise ValidationError({"file": "Save the CSV as UTF-8 and try again."})
    else:
        raise ValidationError({"file": "Use an Excel (.xlsx) or CSV file."})

    while table and not any(str(c or "").strip() for c in table[0]):
        table.pop(0)

    if not table:
        raise ValidationError({"file": "The file is empty."})

    columns = [_key(h) for h in table[0]]
    body = [
        (number, r) for number, r in enumerate(table[1:], start=2)
        if any(str(c or "").strip() for c in r)
    ]

    if len(body) > MAX_ROWS:
        raise ValidationError({"file": f"At most {MAX_ROWS} rows at a time."})

    rows = []

    for number, cells in body:
        row = {"_row": number}

        for column, value in zip(columns, cells):
            if column and column not in row:
                row[column] = value

        rows.append(row)

    return [c for c in columns if c], rows


def _decimal(value, label, errors, *, places):
    if value is None or str(value).strip() == "":
        return None

    try:
        number = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        errors.append(f"{label} must be a number.")

        return None

    if number < 0:
        errors.append(f"{label} cannot be negative.")

        return None

    if number != number.quantize(Decimal(1).scaleb(-places)):
        number = number.quantize(Decimal(1).scaleb(-places))

    return number


def _clean(row, *, mode):
    """Validated values for one row, and its problems."""
    errors = []
    stock_no = str(row.get("stock_no") or "").strip()

    if isinstance(row.get("stock_no"), float) and row["stock_no"].is_integer():
        stock_no = str(int(row["stock_no"]))

    if not stock_no:
        errors.append("Stock No is missing.")

    values = {"stock_no": stock_no}

    for key in TEXT:
        if key in row and row[key] is not None:
            values[key] = str(row[key]).strip()

    for key in MONEY:
        number = _decimal(row.get(key), {"mrp": "MRP", "purchase_price": "Purchase price",
                                          "sale_price": "Sale price"}[key], errors, places=2)

        if number is not None:
            values[key] = number

    reorder = _decimal(row.get("reorder_level"), "Reorder level", errors, places=3)

    if reorder is not None:
        values["reorder_level"] = reorder

    quantity = _decimal(row.get("quantity"), "Quantity", errors, places=3)

    if mode == "prices":
        if not values.get("name"):
            values.pop("name", None)

        if not any(k in values for k in MONEY):
            errors.append("No price on this row.")

        # A file with only a selling price moves the MRP along with it.
        if "sale_price" in values and "mrp" not in values and "mrp" not in row:
            values["mrp"] = values["sale_price"]

        return values, None, errors

    return values, quantity, errors


def run_stock(user, business, upload, *, apply=False):
    """
    Set current stock from a count sheet: ``Stock No`` and ``Quantity``.

    Each product's good stock is brought to the quantity in the file by one
    adjustment movement for the difference (products already at that quantity
    are left alone). Works for any business, branches included, because it
    only changes stock, never the catalogue. Nothing is saved with problems.
    """
    from apps.inventory.models import StockBalance
    from apps.inventory.services.validators import validate_business_available, validate_inventory_access

    validate_business_available(business)
    validate_inventory_access(user, business, write=True)
    columns, rows = read_rows(upload)

    if "stock_no" not in columns:
        raise ValidationError({"file": "There is no Stock No column. Download the template to see the layout."})

    if "quantity" not in columns:
        raise ValidationError({"file": "There is no Quantity column."})

    products = {p.stock_no.lower(): p for p in get_products_for_business(business)}
    have = {b.product_id: b.quantity for b in StockBalance.objects.filter(business=business)}
    seen, errors, plan = {}, [], []
    unchanged = 0

    for row in rows:
        number = row["_row"]
        problems = []
        stock_no = str(row.get("stock_no") or "").strip()

        if isinstance(row.get("stock_no"), float) and row["stock_no"].is_integer():
            stock_no = str(int(row["stock_no"]))

        key = stock_no.lower()
        quantity = _decimal(row.get("quantity"), "Quantity", problems, places=3)

        if not stock_no:
            problems.append("Stock No is missing.")
        elif key in seen:
            problems.append(f"Stock No {stock_no} is also on row {seen[key]}.")
        else:
            seen[key] = number

        if quantity is None and not problems:
            problems.append("Quantity is missing.")

        product = products.get(key)

        if stock_no and product is None:
            problems.append(f"No product has Stock No {stock_no}.")

        if problems:
            errors.extend({"row": number, "message": m} for m in problems)
            continue

        current = have.get(product.pk, Decimal("0"))

        if quantity == current:
            unchanged += 1
        else:
            plan.append((product, quantity, current))

    result = {"created": 0, "updated": len(plan), "unchanged": unchanged, "errors": errors}

    if errors or not apply:
        return result

    with transaction.atomic():
        for product, quantity, current in plan:
            move = StockMovement.MovementType
            create_stock_movement(
                user=user, business=business, product=product,
                movement_type=move.ADJUSTMENT_IN if quantity > current else move.ADJUSTMENT_OUT,
                quantity=abs(quantity - current), reference="Stock import",
                notes=f"Stock set to {quantity} from import (was {current})",
            )

    return result


def run(user, business, upload, *, mode="products", apply=False, can_update=True):
    """
    Check (and, with ``apply``, save) a file. ``mode`` is ``products`` to add
    new products and refresh existing ones, or ``prices`` to change prices only.

    Returns ``{"created", "updated", "unchanged", "errors": [{"row", "message"}]}``;
    nothing is saved while ``errors`` is not empty.
    """
    validate_product_write_access(user, business)
    columns, rows = read_rows(upload)

    if "stock_no" not in columns:
        raise ValidationError({"file": "There is no Stock No column. Download the template to see the layout."})

    if mode == "products" and "name" not in columns:
        raise ValidationError({"file": "There is no Name column. Download the template to see the layout."})

    if mode == "prices" and not any(c in columns for c in MONEY):
        raise ValidationError({"file": "There is no price column (MRP, Purchase Price or Selling Price)."})

    existing = {p.stock_no.lower(): p for p in get_products_for_business(business)}
    seen = {}
    errors, plan = [], []

    for row in rows:
        values, quantity, problems = _clean(row, mode=mode)
        number = row["_row"]
        key = values["stock_no"].lower()

        if key and key in seen:
            problems.append(f"Stock No {values['stock_no']} is also on row {seen[key]}.")
        elif key:
            seen[key] = number

        product = existing.get(key)

        if mode == "prices" and key and product is None:
            problems.append(f"No product has Stock No {values['stock_no']}.")

        if mode == "products" and product is None and not values.get("name"):
            problems.append("Name is missing.")

        if problems:
            errors.extend({"row": number, "message": m} for m in problems)
        else:
            plan.append((number, values, quantity, product))

    result = {
        "created": sum(1 for p in plan if p[3] is None),
        "updated": 0,
        "unchanged": 0,
        "errors": errors,
    }

    changed = []

    for number, values, quantity, product in plan:
        if product is None:
            continue

        diff = {k: v for k, v in values.items()
                if k != "stock_no" and getattr(product, k) != v}

        if mode == "prices":
            diff = {k: v for k, v in diff.items() if k in MONEY or k == "name"}

        if diff and not can_update:
            errors.append({
                "row": number,
                "message": f"{product.stock_no} already exists and you may not change existing products.",
            })
        elif diff:
            changed.append((product, diff))
        else:
            result["unchanged"] += 1

    result["updated"] = len(changed)

    result["errors"] = errors

    if errors or not apply:
        return result

    with transaction.atomic():
        for number, values, quantity, product in plan:
            if product is not None:
                continue

            new = Product(owner_business=business, **values)

            try:
                new.full_clean()
            except DjangoValidationError as error:
                raise ValidationError({"file": f"Row {number}: " + "; ".join(
                    m for ms in error.message_dict.values() for m in ms)})

            new.save()

            if quantity:
                create_stock_movement(
                    user=user, business=business, product=new,
                    movement_type=StockMovement.MovementType.OPENING,
                    quantity=quantity, reference="Import", notes="Opening stock from import",
                )

        for product, diff in changed:
            for k, v in diff.items():
                setattr(product, k, v)

            product.full_clean()
            product.save()

    return result


# -- files going out -----------------------------------------------------------

def _workbook(header, body, widths=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    book = Workbook()
    sheet = book.active
    sheet.title = "Products"
    sheet.append(header)

    for cell in sheet[1]:
        cell.font = Font(bold=True)

    for line in body:
        sheet.append(line)

    # Text that starts with = + - @ is kept as text, never run as a formula.
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, str) and cell.value[:1] in ("=", "+", "-", "@"):
                cell.data_type = "s"

    for i, label in enumerate(header, start=1):
        sheet.column_dimensions[sheet.cell(row=1, column=i).column_letter].width = max(12, len(label) + 4)

    sheet.freeze_panes = "A2"
    out = io.BytesIO()
    book.save(out)

    return out.getvalue()


def export_products(business, *, with_cost=True):
    """Every product with its stock here, laid out so it can be imported again."""
    from apps.inventory.models import StockBalance

    stock = {
        b.product_id: b.quantity
        for b in StockBalance.objects.filter(business=business)
    }
    columns = [c for c in EXPORT_COLUMNS if with_cost or c[0] != "purchase_price"]
    body = []

    for p in get_products_for_business(business):
        line = []

        for key, _ in columns:
            value = stock.get(p.pk, Decimal("0")) if key == "quantity" else getattr(p, key)
            line.append(float(value) if isinstance(value, Decimal) else value)

        body.append(line)

    return _workbook([label for _, label in columns], body)


def prices_template():
    return _workbook(
        ["Stock No", "Stock Name", "Purchase Price", "Selling Price"],
        [["BRK-001", "Brake pad (front)", 300, 400]],
    )


def stock_template():
    return _workbook(["Stock No", "Quantity"], [["BRK-001", 20]])


def template():
    header = [label for _, label in EXPORT_COLUMNS]
    sample = ["BRK-001", "Brake pad (front)", "Honda", "Shine", "Spares", "Brakes", "pcs",
              450, 300, 400, 5, 20]

    return _workbook(header, [sample])
