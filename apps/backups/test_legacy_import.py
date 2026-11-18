import json

from django.core.files.uploadedfile import SimpleUploadedFile

from apps.backups.models import BackupLog
from apps.finance.models import Expense, Income
from apps.inventory.models import Product, StockBalance
from apps.purchases.models import Purchase, PurchasePayment
from apps.sales.models import Followup, Sale
from rest_framework.test import APITestCase

from apps.common.testing import TenantTestMixin

URL = "/api/v1/backups/"


def row(_model, _pk, **fields):
    return {"model": _model, "pk": _pk, "fields": fields}


def sample():
    sale = dict(
        sale_ref="SALE-000001", sale_date="2026-02-04T18:15:00Z", customer_name="Ram",
        contact_no="9800000001", is_servicing=True, is_fitting=False, is_accident=False,
        km_driven="1200", bike_registration_no="BA 1 PA 2", job_done_on_vehicle="Oil change",
        remarks="x", labour_charge=100.0, grand_total=1300.0, discount_amount=50.0,
        net_total=1250.0, paid_amount=1000.0, paid_from="cash", bill_no="1",
    )

    return [
        row("core.category", 1, name="Spare"),
        row("core.stock", 1, item_no="A-1", name="Oil", category=1, group=None, model="X",
            stock=5, purchase_price=100.0, sale_price=150.0, block="A"),
        row("core.stock", 2, item_no="a-1", name="Oil dup", category=1, group=None, model="X",
            stock=1, purchase_price=100.0, sale_price=150.0, block=None),
        row("core.stock", 3, item_no="B-2", name="Plug", category=1, group=None, model="X",
            stock=0, purchase_price=10.0, sale_price=20.0, block=None),
        row("core.supplier", 1, name="Vg Pvt", contact="01", address="KTM", email=None),
        row("core.purchase", 1, supplier=1, date="2026-02-01T00:00:00Z", bill_no="B1",
            grand_total=500.0, discount_amount=0, transit_discount_amount=0, vat_percentage=13.0,
            vat_amount=65.0, net_total=565.0, paid_amount=565.0, paid_from="cash"),
        row("core.purchaseitem", 1, purchase=1, item=1, quantity=5, price=100.0),
        row("core.sale", 1, **sale),
        row("core.saleitem", 1, sale=1, item=1, quantity=1, sale_price=1200.0, total_price=1200.0),
        row("core.followupdashboard", 1, sale=1, follow_up_date="2026-05-06", status="pending",
            reason=None, remarks="auto", terminated_at=None),
        row("core.expense", 1, title="Tea", expense_type="operational", amount=50.0,
            expense_date="2026-02-02", payment_mode="cash", reference_type=None, note=None),
        row("core.expense", 2, title="Pay", expense_type="operational", amount=565.0,
            expense_date="2026-02-02", payment_mode="cash", reference_type="Purchase", note=None),
        row("core.staff", 1, name="S"),
    ]


def upload(rows):
    return SimpleUploadedFile("data.json", json.dumps(rows).encode(), "application/json")


class LegacyImportTests(TenantTestMixin, APITestCase):
    """The import goes into OTHER-HO: a Head Office with no sales yet."""

    def setUp(self):
        super().setUp()
        self.authenticate("OTHER-HO", "owner")
        self.biz = self.other_ho

    def post(self, path, rows=None, **extra):
        return self.client.post(
            URL + path, {"file": upload(rows if rows is not None else sample()), **extra},
            format="multipart",
        )

    def test_preview_changes_nothing_and_lists_what_is_skipped(self):
        r = self.post("legacy-preview/")

        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["created"]["products"], 3)
        self.assertEqual(r.json()["not_imported"], [{"what": "staff records (add them in Staff)", "count": 1}])
        self.assertFalse(Sale.objects.filter(business=self.biz).exists())
        self.assertFalse(Product.objects.filter(owner_business=self.biz).exists())

    def test_import_needs_the_business_code(self):
        self.assertEqual(self.post("legacy-import/").status_code, 400)
        self.assertEqual(self.post("legacy-import/", confirm="WRONG").status_code, 400)

    def test_import_writes_history_keeps_a_safety_copy_and_moves_no_stock(self):
        r = self.post("legacy-import/", confirm=self.biz.business_code)

        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["safety_copy"])
        self.assertTrue(BackupLog.objects.filter(business=self.biz, kind="safety").exists())

        oil = Product.objects.get(owner_business=self.biz, stock_no="A-1")
        self.assertEqual(StockBalance.objects.get(product=oil, business=self.biz).quantity, 5)
        # a-1 is a different item (another name) on the same number: kept apart.
        dup = Product.objects.get(owner_business=self.biz, name="Oil dup")
        self.assertEqual(dup.stock_no, "a-1 (2)")
        self.assertEqual(StockBalance.objects.get(product=dup, business=self.biz).quantity, 1)

        sale = Sale.objects.get(business=self.biz)
        self.assertEqual(sale.status, "confirmed")
        self.assertEqual(sale.sale_type, "maintenance")
        self.assertEqual(sale.payments.count(), 1)
        self.assertEqual(Purchase.objects.get(business=self.biz).payments.count(), 1)
        self.assertTrue(Followup.objects.filter(business=self.biz, sale=sale).exists())
        self.assertTrue(Expense.objects.filter(business=self.biz).exists())

    def test_a_second_import_is_refused_so_nothing_is_entered_twice(self):
        self.post("legacy-import/", confirm=self.biz.business_code)
        r = self.post("legacy-import/", confirm=self.biz.business_code)

        self.assertEqual(r.status_code, 400)
        self.assertIn("already has", r.json()["detail"])
        self.assertEqual(Sale.objects.filter(business=self.biz).count(), 1)

    def test_a_negative_purchase_is_skipped_with_a_warning(self):
        rows = sample() + [
            row("core.purchase", 2, supplier=1, date="2026-02-02T00:00:00Z", bill_no="B2",
                grand_total=-100.0, discount_amount=0, transit_discount_amount=0, vat_percentage=13.0,
                vat_amount=-13.0, net_total=-113.0, paid_amount=113.0, paid_from="cash"),
            row("core.purchaseitem", 2, purchase=2, item=1, quantity=-1, price=100.0),
        ]
        r = self.post("legacy-import/", rows, confirm=self.biz.business_code)

        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Purchase.objects.filter(business=self.biz).count(), 1)
        self.assertTrue(any("negative" in w for w in r.json()["warnings"]))

    def test_a_bad_file_is_a_clear_error(self):
        bad = SimpleUploadedFile("d.json", b"not json", "application/json")
        r = self.client.post(URL + "legacy-preview/", {"file": bad}, format="multipart")

        self.assertEqual(r.status_code, 400)
