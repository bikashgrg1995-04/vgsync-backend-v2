from apps.accounts.models import User
from apps.sales.test_sale_returns import ReturnTestCase


class HeadOfficeStaffAuditTests(ReturnTestCase):
    """What a Head Office staff position can and cannot reach."""

    def setUp(self):
        super().setUp()
        seller = self.make_position(self.ho, "Seller", sales=("view", "create"))
        books = self.make_position(self.ho, "Books", reports=("view",), sales=("view",))
        self.seller = self.make_user("seller", User.Role.STAFF, company=self.company, position=seller)
        self.books = self.make_user("books", User.Role.STAFF, company=self.company, position=books)

    def get(self, user, path):
        self.authenticate("ACME-HO", user)

        return self.client.get(path)

    def test_a_seller_can_pick_products_but_never_sees_what_they_cost(self):
        response = self.get("seller", "/api/v1/inventory/stock/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json())
        for row in response.json():
            self.assertNotIn("purchase_price", row)
            self.assertIn("sale_price", row)

    def test_a_seller_still_cannot_open_the_product_catalogue(self):
        self.assertEqual(self.get("seller", "/api/v1/inventory/products/").status_code, 403)

    def test_someone_with_no_sales_or_stock_rights_gets_no_stock_list(self):
        nobody = self.make_position(self.ho, "Viewer", reports=("view",))
        self.make_user("viewer", User.Role.STAFF, company=self.company, position=nobody)

        self.assertEqual(self.get("viewer", "/api/v1/inventory/stock/").status_code, 403)

    def test_payroll_summary_is_only_for_the_owner(self):
        self.assertEqual(self.get("books", "/api/v1/reports/payroll-summary/").status_code, 403)
        self.assertEqual(self.get("books", "/api/v1/reports/profit-loss/").status_code, 200)

        self.authenticate("ACME-HO", "owner")
        self.assertEqual(self.client.get("/api/v1/reports/payroll-summary/").status_code, 200)

    def test_staff_never_see_another_branch_by_sending_a_header(self):
        self.authenticate("ACME-HO", "books")
        response = self.client.get("/api/v1/sales/", HTTP_X_BUSINESS_ID=str(self.branch.pk))

        self.assertEqual(response.status_code, 200)
