import csv
import io

from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import User
from apps.purchases.test_purchases import BASE, PurchaseTestCase


def sheet(rows, header=("Stock No", "Quantity", "Unit Price")):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)

    return SimpleUploadedFile("po.csv", out.getvalue().encode(), "text/csv")


class ImportLinesTests(PurchaseTestCase):
    def read(self, upload):
        return self.client.post(BASE + "import-lines/", {"file": upload}, format="multipart")

    def test_lines_come_back_ready_for_a_new_purchase(self):
        data = self.read(sheet([["OIL-1", "12", "95"], ["flt-1", "3", "40"]])).json()

        self.assertEqual(data["errors"], [])
        self.assertEqual(
            [(l["product"], l["quantity"], l["unit_price"]) for l in data["lines"]],
            [(self.oil.pk, "12.000", "95.00"), (self.filter.pk, "3.000", "40.00")],
        )

        # The lines go straight into the normal create.
        self.assertEqual(self.create(items=[
            {"product": l["product"], "quantity": l["quantity"], "unit_price": l["unit_price"]}
            for l in data["lines"]
        ]).status_code, 201)

    def test_problems_are_listed_by_row_and_no_lines_returned(self):
        data = self.read(sheet([["OIL-1", "0", "5"], ["NOPE", "1", "5"], ["X-1", "1", "5"],
                                ["FLT-1", "2", "5"], ["FLT-1", "2", "5"]])).json()

        self.assertEqual(data["lines"], [])
        self.assertEqual([e["row"] for e in data["errors"]], [2, 3, 4, 6])

    def test_a_line_with_no_price_anywhere_is_an_error(self):
        data = self.read(sheet([["FLT-1", "2", ""]])).json()

        self.assertEqual([e["row"] for e in data["errors"]], [2])

    def test_needs_stock_no_and_quantity_columns(self):
        self.assertEqual(self.read(sheet([["a"]], header=("Name",))).status_code, 400)

    def test_needs_the_create_permission(self):
        position = self.make_position(self.ho, "Viewer", purchases=("view",))
        self.make_user("viewer", User.Role.STAFF, business=self.ho, position=position)
        self.authenticate("ACME-HO", "viewer")

        self.assertEqual(self.read(sheet([["OIL-1", "1", "5"]])).status_code, 403)
