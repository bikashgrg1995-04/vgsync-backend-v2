import io
from decimal import Decimal

from django.test import SimpleTestCase
from openpyxl import load_workbook

from apps.common import export
from apps.purchases.test_returns import ReturnTestCase as PurchaseReturnTestCase
from apps.sales.test_sale_returns import ReturnTestCase

XLSX = export.XLSX_TYPE


def workbook(response):
    return load_workbook(io.BytesIO(response.content))


def cells(sheet):
    return [[c.value for c in row] for row in sheet.iter_rows()]


class DocumentTests(SimpleTestCase):
    def document(self, **extra):
        return export.Document(
            title="Test report",
            lines=["Business: Acme"],
            summary=[("Total", "1500.00"), ("Count", 3)],
            tables=[
                export.Table(
                    "Rows",
                    [export.Column("Name"), export.Column("Amount", export.MONEY),
                     export.Column("Qty", export.QTY), export.Column("Number", export.INT)],
                    [["Bolt", "1234.50", "2.500", 7], ["Nut", "0.00", "0.000", 0]],
                )
            ],
            **extra,
        )

    def test_excel_has_a_summary_sheet_and_one_sheet_per_table(self):
        response = export.respond(self.document(filename="demo"), "xlsx")
        book = workbook(response)

        self.assertEqual(book.sheetnames, ["Summary", "Rows"])
        summary = cells(book["Summary"])
        self.assertEqual(summary[0][0], "Test report")
        self.assertIn(["Total", 1500.0], [r[:2] for r in summary])

    def test_numbers_are_real_numbers_and_text_stays_text(self):
        book = workbook(export.respond(self.document(), "xlsx"))
        rows = cells(book["Rows"])

        self.assertEqual(rows[3], ["Bolt", 1234.5, 2.5, 7])
        self.assertEqual(rows[4], ["Nut", 0.0, 0.0, 0])
        self.assertEqual(book["Rows"]["B4"].number_format, "#,##0.00")

    def test_text_that_looks_like_a_formula_is_never_run(self):
        document = export.Document(
            title="x",
            tables=[export.Table("T", [export.Column("Name")], [["=1+1"], ["+cmd"], ["@sum"]])],
        )
        sheet = workbook(export.respond(document, "xlsx"))["T"]

        for row in (4, 5, 6):
            self.assertEqual(sheet.cell(row=row, column=1).data_type, "s")

        self.assertEqual(sheet["A4"].value, "=1+1")

    def test_sheet_names_are_safe_and_unique(self):
        document = export.Document(
            title="x",
            tables=[
                export.Table("A/B:C*" + "x" * 40, [export.Column("a")], []),
                export.Table("A/B:C*" + "x" * 40, [export.Column("a")], []),
                export.Table("Summary", [export.Column("a")], []),
            ],
        )

        names = workbook(export.respond(document, "xlsx")).sheetnames

        self.assertEqual(len(names), len(set(n.lower() for n in names)))
        self.assertTrue(all(len(n) <= 31 for n in names))
        self.assertFalse(any(c in n for n in names for c in "[]:*?/\\"))

    def test_the_pdf_is_a_pdf_and_survives_odd_text(self):
        document = self.document()
        document.title = "Tom & <Jerry> report"
        document.tables[0].rows.append(["<b>bold</b> & more", "x", None, None])

        response = export.respond(document, "pdf")

        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_an_empty_table_still_exports(self):
        document = export.Document(
            title="Empty", tables=[export.Table("None here", [export.Column("a")], [])]
        )

        self.assertTrue(export.respond(document, "pdf").content.startswith(b"%PDF"))
        self.assertEqual(workbook(export.respond(document, "xlsx")).sheetnames, ["Summary", "None here"])

    def test_a_long_table_runs_over_pages(self):
        rows = [[f"Item {n}", "10.00"] for n in range(400)]
        document = export.Document(
            title="Long",
            tables=[export.Table("Rows", [export.Column("Name"), export.Column("Amount", export.MONEY)], rows)],
        )

        pdf = export.respond(document, "pdf").content

        self.assertGreater(pdf.count(b"/Type /Page\n") + pdf.count(b"/Type /Page "), 3)

    def test_the_file_name_carries_the_report_and_date(self):
        response = export.respond(self.document(filename="demo"), "xlsx")

        self.assertRegex(
            response["Content-Disposition"], r'attachment; filename="demo-\d{8}\.xlsx"'
        )


class SalesReportExportTests(ReturnTestCase):
    def get(self, url, kind, **params):
        return self.client.get(url, {"export": kind, **params})

    def setUp(self):
        super().setUp()
        sale = self.confirmed()
        self.pay(sale["id"], "500.00")
        self.client.post(
            "/api/v1/finance/expenses/",
            {"category": "rent", "amount": "200.00"},
            format="json",
        )

    def test_every_report_downloads_as_excel_and_pdf(self):
        urls = [
            "/api/v1/reports/profit-loss/",
            "/api/v1/reports/receivables/",
            "/api/v1/reports/payables/",
            "/api/v1/reports/stock-value/",
            "/api/v1/reports/sales-trend/",
            "/api/v1/reports/top-products/",
            "/api/v1/reports/top-customers/",
            "/api/v1/reports/top-suppliers/",
            "/api/v1/reports/payroll-summary/",
        ]

        for url in urls:
            excel = self.get(url, "xlsx")
            pdf = self.get(url, "pdf")

            self.assertEqual(excel.status_code, 200, url)
            self.assertEqual(excel["Content-Type"], XLSX, url)
            self.assertEqual(workbook(excel).sheetnames[0], "Summary", url)
            self.assertEqual(pdf.status_code, 200, url)
            self.assertTrue(pdf.content.startswith(b"%PDF"), url)

    def test_the_profit_loss_sheet_matches_the_json(self):
        data = self.client.get("/api/v1/reports/profit-loss/").json()

        book = workbook(self.get("/api/v1/reports/profit-loss/", "xlsx"))
        summary = {r[0]: r[1] for r in cells(book["Summary"]) if r[0] and r[1] is not None}

        self.assertEqual(Decimal(str(summary["Profit"])), Decimal(data["profit"]))
        self.assertEqual(Decimal(str(summary["Income"])), Decimal(data["income"]["total"]))

    def test_the_receivables_sheet_lists_the_customer(self):
        book = workbook(self.get("/api/v1/reports/receivables/", "xlsx"))
        rows = cells(book["Customers"])

        self.assertEqual(rows[2][0], "Customer")
        self.assertEqual(rows[3][0], "Ram Auto")
        self.assertEqual(rows[3][-1], 1000.0)

    def test_the_period_is_stated(self):
        book = workbook(
            self.get("/api/v1/reports/profit-loss/", "xlsx", date_from="2026-01-01", date_to="2026-12-31")
        )

        self.assertEqual(cells(book["Summary"])[2][0], "Period: 2026-01-01 to 2026-12-31")

    def test_filters_still_apply_to_a_download(self):
        book = workbook(self.get("/api/v1/reports/top-products/", "xlsx", limit=1))

        self.assertEqual(len(cells(book["Products"])), 4)  # title, blank, header, 1 row

    def test_a_bad_export_type_is_refused(self):
        self.assertEqual(self.get("/api/v1/reports/profit-loss/", "csv").status_code, 400)

    def test_bad_filters_are_refused_before_any_file(self):
        self.assertEqual(
            self.get("/api/v1/reports/profit-loss/", "pdf", date_from="nope").status_code, 400
        )

    def test_no_export_gives_json_as_before(self):
        response = self.client.get("/api/v1/reports/profit-loss/")

        self.assertEqual(response["Content-Type"], "application/json")

    def test_only_the_owner_can_download(self):
        self.make_user("boss", "admin", company=self.company)
        self.authenticate("ACME-HO", "boss")

        self.assertEqual(self.get("/api/v1/reports/profit-loss/", "pdf").status_code, 403)

        self.client.credentials()
        self.assertEqual(self.get("/api/v1/reports/profit-loss/", "pdf").status_code, 401)

    def test_a_customer_statement_downloads_every_row(self):
        url = f"/api/v1/customers/{self.customer.pk}/statement/"
        data = self.client.get(url).json()

        excel = self.get(url, "xlsx")
        book = workbook(excel)
        rows = cells(book["Transactions"])

        self.assertEqual(excel["Content-Type"], XLSX)
        self.assertIn(f"customer-statement-{self.customer.pk}", excel["Content-Disposition"])
        self.assertEqual(len(rows) - 3, len(data["results"]))
        self.assertEqual(rows[-1][-1], float(data["closing_balance"]))
        self.assertTrue(self.get(url, "pdf").content.startswith(b"%PDF"))

    def test_a_statement_download_respects_its_filters(self):
        url = f"/api/v1/customers/{self.customer.pk}/statement/"

        only_payments = workbook(self.get(url, "xlsx", type="payment"))
        rows = cells(only_payments["Transactions"])

        self.assertEqual({r[1] for r in rows[3:]}, {"payment"})

    def test_a_statement_of_another_business_is_not_found(self):
        url = f"/api/v1/customers/{self.other_customer.pk}/statement/"

        self.assertEqual(self.get(url, "pdf").status_code, 404)

    def test_a_bad_export_type_on_a_statement_is_refused(self):
        url = f"/api/v1/customers/{self.customer.pk}/statement/"

        self.assertEqual(self.get(url, "doc").status_code, 400)


class SupplierExportTests(PurchaseReturnTestCase):
    def test_a_supplier_statement_downloads(self):
        purchase = self.received()
        self.pay(purchase["id"], "300.00")
        url = f"/api/v1/suppliers/{self.supplier.pk}/statement/"
        data = self.client.get(url).json()

        response = self.client.get(url, {"export": "xlsx"})
        rows = cells(workbook(response)["Transactions"])

        self.assertEqual(response.status_code, 200)
        self.assertIn(f"supplier-statement-{self.supplier.pk}", response["Content-Disposition"])
        self.assertEqual(len(rows) - 3, len(data["results"]))
        self.assertEqual(rows[-1][-1], float(data["closing_balance"]))

    def test_payables_download(self):
        self.received()

        response = self.client.get("/api/v1/reports/payables/", {"export": "pdf"})

        self.assertTrue(response.content.startswith(b"%PDF"))
