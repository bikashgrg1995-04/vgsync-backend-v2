import csv
import io
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import User
from apps.inventory.models import Product, StockBalance, StockMovement
from apps.sales.test_sale_returns import ReturnTestCase

BASE = "/api/v1/inventory/products/"


def sheet(rows, header=("Stock No", "Name", "MRP", "Purchase Price", "Sale Price", "Quantity")):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)

    return SimpleUploadedFile("p.csv", out.getvalue().encode(), "text/csv")


def xlsx(rows, header=("Stock No", "Name", "MRP")):
    from openpyxl import Workbook

    book = Workbook()
    book.active.append(list(header))

    for r in rows:
        book.active.append(list(r))

    out = io.BytesIO()
    book.save(out)

    return SimpleUploadedFile("p.xlsx", out.getvalue())


class ImportTests(ReturnTestCase):
    def post(self, upload, **extra):
        return self.client.post(
            BASE + "import/", {"file": upload, **extra}, format="multipart"
        )

    def test_a_preview_changes_nothing(self):
        before = Product.objects.count()
        data = self.post(sheet([["N-1", "Oil", "100", "60", "90", "5"]])).json()

        self.assertEqual((data["created"], data["errors"]), (1, []))
        self.assertFalse(data["applied"])
        self.assertEqual(Product.objects.count(), before)

    def test_applying_adds_products_with_opening_stock(self):
        data = self.post(
            sheet([["N-1", "Oil", "100", "60", "90", "5"], ["N-2", "Plug", "50", "", "", ""]]),
            apply="true",
        ).json()

        self.assertTrue(data["applied"], data)
        oil = Product.objects.get(stock_no="N-1", owner_business=self.ho)
        self.assertEqual((oil.mrp, oil.purchase_price, oil.sale_price), (Decimal("100"), Decimal("60"), Decimal("90")))
        self.assertEqual(StockBalance.objects.get(product=oil, business=self.ho).quantity, Decimal("5"))
        self.assertEqual(
            StockMovement.objects.get(stock_balance__product=oil).movement_type, "opening"
        )
        self.assertFalse(StockBalance.objects.filter(product__stock_no="N-2").exists())

    def test_one_bad_row_saves_nothing_and_names_the_row(self):
        before = Product.objects.count()
        data = self.post(
            sheet([["N-1", "Oil", "100", "", "", ""], ["N-2", "Plug", "abc", "", "", ""],
                   ["", "Nameless", "", "", "", ""]]),
            apply="true",
        ).json()

        self.assertFalse(data["applied"])
        self.assertEqual([e["row"] for e in data["errors"]], [3, 4])
        self.assertEqual(Product.objects.count(), before)

    def test_a_repeated_stock_no_in_the_file_is_an_error(self):
        data = self.post(sheet([["N-1", "A", "", "", "", ""], ["n-1", "B", "", "", "", ""]])).json()

        self.assertEqual(len(data["errors"]), 1)
        self.assertIn("row 2", data["errors"][0]["message"])

    def test_existing_products_are_updated_and_their_stock_is_left_alone(self):
        self.post(sheet([["N-1", "Oil", "100", "", "", "5"]]), apply="true")
        data = self.post(sheet([["N-1", "Engine oil", "120", "", "", "99"]]), apply="true").json()

        self.assertEqual((data["created"], data["updated"]), (0, 1))
        oil = Product.objects.get(stock_no="N-1", owner_business=self.ho)
        self.assertEqual((oil.name, oil.mrp), ("Engine oil", Decimal("120")))
        self.assertEqual(StockBalance.objects.get(product=oil).quantity, Decimal("5"))

    def test_update_mrp_changes_prices_only_and_needs_the_product_to_exist(self):
        self.post(sheet([["N-1", "Oil", "100", "60", "90", ""]]), apply="true")

        data = self.post(xlsx([["N-1", "Renamed", 130], ["NOPE", "x", 5]]), mode="prices").json()
        self.assertEqual([e["row"] for e in data["errors"]], [3])

        data = self.post(xlsx([["N-1", "Renamed", 130]]), mode="prices", apply="true").json()
        self.assertTrue(data["applied"], data)
        oil = Product.objects.get(stock_no="N-1", owner_business=self.ho)
        self.assertEqual((oil.mrp, oil.name), (Decimal("130"), "Oil"))

    def test_excel_and_loose_header_names_work(self):
        data = self.post(
            xlsx([["N-9", "Chain", 75]], header=("SKU", "Item", "M.R.P")), apply="true"
        ).json()

        self.assertTrue(data["applied"], data)
        self.assertEqual(Product.objects.get(stock_no="N-9").mrp, Decimal("75"))

    def test_wrong_files_are_refused_with_a_reason(self):
        bad = SimpleUploadedFile("x.pdf", b"x")
        self.assertEqual(self.post(bad).status_code, 400)
        self.assertEqual(self.post(sheet([], header=("Name", "MRP"))).status_code, 400)

    def test_export_round_trips_through_import(self):
        from openpyxl import load_workbook

        self.post(sheet([["N-1", "Oil", "100", "60", "90", "5"]]), apply="true")
        response = self.client.get(BASE + "export/")

        self.assertEqual(response.status_code, 200)
        rows = list(load_workbook(io.BytesIO(response.content)).active.values)
        self.assertEqual(rows[0][0], "Stock No")
        self.assertIn("N-1", [r[0] for r in rows[1:]])

        again = self.post(SimpleUploadedFile("e.xlsx", response.content), apply="true").json()
        self.assertEqual((again["created"], again["updated"], again["errors"]), (0, 0, []))

    def test_template_downloads(self):
        self.assertEqual(self.client.get(BASE + "import-template/").status_code, 200)

    def test_staff_need_the_right_permissions(self):
        position = self.make_position(self.ho, "Viewer", inventory=("view",))
        self.make_user("viewer", User.Role.STAFF, business=self.ho, position=position)
        self.authenticate("ACME-HO", "viewer")

        self.assertEqual(self.client.get(BASE + "export/").status_code, 200)
        self.assertEqual(self.post(sheet([["N-1", "Oil", "1", "", "", ""]])).status_code, 403)

    def test_a_branch_cannot_import_into_the_head_office_catalogue(self):
        self.make_user("bowner", User.Role.OWNER, business=self.branch)
        self.authenticate("ACME-BR", "bowner")

        self.assertEqual(self.post(sheet([["N-1", "Oil", "1", "", "", ""]])).status_code, 403)
