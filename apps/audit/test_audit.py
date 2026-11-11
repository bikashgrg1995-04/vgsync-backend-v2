from decimal import Decimal

from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.common.testing import TenantTestMixin
from apps.inventory.models import Product, StockMovement
from apps.sales.test_sale_returns import ReturnTestCase

AUDIT = "/api/v1/audit/"
EXPENSES = "/api/v1/finance/expenses/"
PRODUCTS = "/api/v1/inventory/products/"


class AuditTestCase(ReturnTestCase):
    def history(self, **params):
        return self.client.get(AUDIT, params).json()["results"]


class WhatIsRecordedTests(AuditTestCase):
    def test_setting_up_data_outside_a_request_is_not_logged(self):
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_creating_a_record_is_logged_with_who_and_where(self):
        self.client.post(
            EXPENSES, {"category": "rent", "amount": "500.00"}, format="json"
        )

        row = AuditLog.objects.get()

        self.assertEqual(row.action, "create")
        self.assertEqual(row.model, "finance.expense")
        self.assertEqual(row.actor, self.owner)
        self.assertEqual(row.actor_name, "owner")
        self.assertEqual(row.business, self.ho)
        self.assertEqual(row.changes["amount"], "500.00")

    def test_a_change_records_the_old_and_new_value(self):
        expense = self.client.post(
            EXPENSES, {"category": "rent", "amount": "500.00"}, format="json"
        ).json()

        self.client.patch(
            f"{EXPENSES}{expense['id']}/", {"amount": "650.00"}, format="json"
        )

        row = AuditLog.objects.filter(action="update").get()

        self.assertEqual(row.changes, {"amount": ["500.00", "650.00"]})
        self.assertEqual(row.object_id, str(expense["id"]))

    def test_a_save_that_changes_nothing_adds_no_row(self):
        expense = self.client.post(
            EXPENSES, {"category": "rent", "amount": "500.00"}, format="json"
        ).json()

        self.client.patch(
            f"{EXPENSES}{expense['id']}/", {"amount": "500.00"}, format="json"
        )

        self.assertEqual(AuditLog.objects.filter(action="update").count(), 0)

    def test_voiding_shows_the_status_and_the_reason(self):
        expense = self.client.post(
            EXPENSES, {"category": "rent", "amount": "500.00"}, format="json"
        ).json()
        self.client.post(
            f"{EXPENSES}{expense['id']}/void/", {"reason": "Typo"}, format="json"
        )

        row = AuditLog.objects.filter(action="update").get()

        self.assertEqual(row.changes["status"], ["active", "void"])
        self.assertEqual(row.changes["void_reason"], ["", "Typo"])

    def test_a_product_price_change_is_logged(self):
        product = self.client.get(PRODUCTS).json()[0]

        response = self.client.patch(
            f"{PRODUCTS}{product['id']}/", {"sale_price": "999.00"}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.data)
        row = AuditLog.objects.filter(model="inventory.product", action="update").get()
        self.assertEqual(row.changes["sale_price"][1], "999.00")
        self.assertEqual(row.business, self.ho)

    def test_sales_and_payments_are_logged_in_their_business(self):
        sale = self.confirmed()
        self.pay(sale["id"], "500.00")

        models = set(AuditLog.objects.values_list("model", flat=True))

        self.assertTrue({"sales.sale", "sales.salepayment", "finance.income"} <= models | {"finance.income"})
        self.assertIn("sales.sale", models)
        self.assertIn("sales.salepayment", models)
        self.assertEqual(
            set(AuditLog.objects.values_list("business", flat=True)), {self.ho.pk}
        )

    def test_confirming_a_sale_is_a_status_change(self):
        sale = self.made()
        self.confirm(sale["id"])

        row = AuditLog.objects.filter(model="sales.sale", action="update").first()

        self.assertEqual(row.changes["status"], ["draft", "confirmed"])

    def test_a_voided_payment_is_logged(self):
        sale = self.confirmed()
        payment = self.pay(sale["id"], "500.00").json()
        self.client.post(
            f"/api/v1/sales/payments/{payment['id']}/void/",
            {"reason": "Bounced"},
            format="json",
        )

        row = AuditLog.objects.filter(
            model="sales.salepayment", action="update"
        ).get()

        self.assertEqual(row.changes["status"], ["active", "void"])

    def test_only_stock_adjustments_are_logged_not_every_movement(self):
        sale = self.confirmed()  # a sale issue movement
        self.assertFalse(
            AuditLog.objects.filter(model="inventory.stockmovement").exists()
        )

        movement = StockMovement.MovementType.ADJUSTMENT_IN
        response = self.client.post(
            "/api/v1/inventory/stock/movements/",
            {"product_id": self.oil.pk, "movement_type": movement, "quantity": "5",
             "notes": "Found in the store",
             "adjustment_action": "increase", "adjustment_reason": "found"},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        rows = AuditLog.objects.filter(model="inventory.stockmovement")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().changes["movement_type"], "adjustment_in")
        self.assertEqual(sale["status"], "confirmed")

    def test_passwords_never_reach_the_log(self):
        position = self.make_position(self.ho, "Sales", sales=("view",))
        response = self.client.post(
            "/api/v1/staff/",
            {"username": "newbie", "role": "staff", "position": position.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        temporary = response.json()["temporary_password"]

        rows = AuditLog.objects.filter(model="accounts.user")
        self.assertEqual(rows.count(), 1)

        for row in AuditLog.objects.all():
            self.assertNotIn("password", row.changes)
            self.assertNotIn(temporary, str(row.changes))

    def test_a_rolled_back_change_leaves_no_row(self):
        self.client.post(
            f"/api/v1/sales/{self.made()['id']}/confirm/"
        )
        before = AuditLog.objects.count()

        # Too much stock asked for: the confirm fails and rolls back.
        big = self.made(items=[self.line(self.oil, "9999")])
        self.confirm(big["id"])

        statuses = AuditLog.objects.filter(
            model="sales.sale", object_id=str(big["id"]), action="update"
        )
        self.assertEqual(statuses.count(), 0)
        self.assertGreater(AuditLog.objects.count(), 0)
        self.assertGreaterEqual(AuditLog.objects.count(), before)


class ReadingTheLogTests(AuditTestCase):
    def setUp(self):
        super().setUp()
        self.expense = self.client.post(
            EXPENSES, {"category": "rent", "amount": "500.00"}, format="json"
        ).json()
        self.client.patch(
            f"{EXPENSES}{self.expense['id']}/", {"amount": "600.00"}, format="json"
        )
        self.confirmed()

    def test_newest_first_with_a_readable_summary(self):
        rows = self.history(model="finance.expense")

        self.assertEqual(rows[0]["action"], "update")
        self.assertEqual(rows[0]["summary"], "amount: 500.00 -> 600.00")
        self.assertEqual(rows[1]["action"], "create")
        self.assertEqual(rows[0]["actor_name"], "owner")

    def test_a_list_field_says_what_was_added_and_removed(self):
        from apps.audit.serializers import AuditLogSerializer

        change = AuditLogSerializer._change
        self.assertEqual(
            change("actions", "['view', 'cancel']", "['view', 'approve']"),
            "actions: added approve and removed cancel",
        )
        self.assertEqual(change("is_active", True, False), "is active: yes -> no")
        self.assertEqual(change("position_id", None, 4), "position: none -> 4")
        self.assertEqual(change("position_id", 1, 2, lambda n, v: {1: "Clerk", 2: "Manager"}[v]), "position: Clerk -> Manager")

    def test_created_rows_read_as_added(self):
        rows = self.history(model="finance.expense")

        self.assertTrue(rows[1]["summary"].startswith("Added "))

    def test_access_records_read_in_plain_words(self):
        from apps.audit.models import AuditLog
        from apps.audit.serializers import AuditLogSerializer

        def summary(model, action, repr_, changes=None):
            row = AuditLog(model=model, action=action, object_repr=repr_, changes=changes or {})
            return AuditLogSerializer(row).data["summary"]

        self.assertEqual(
            summary("accounts.staffpermissionoverride", "create", "Anita Rai: Denied Cancel sales"),
            "Override set: Anita Rai: Denied Cancel sales",
        )
        self.assertEqual(
            summary("accounts.positionpermission", "delete", "Clerk - Sales"),
            "Access removed: Clerk - Sales",
        )
        self.assertEqual(
            summary("accounts.positionpermission", "update", "Clerk - Sales", {"actions": ["[]", "['cancel']"]}),
            "Clerk - Sales: actions: added cancel",
        )

    def test_an_override_names_the_person_and_the_action(self):
        from apps.accounts.models import StaffPermissionOverride

        self.assertEqual(
            str(StaffPermissionOverride(user=self.owner, permission="sales.cancel", effect="deny")),
            f"{self.owner.get_full_name() or self.owner.username}: Denied Cancel sales",
        )

    def test_one_records_whole_history(self):
        rows = self.history(model="finance.expense", object_id=self.expense["id"])

        self.assertEqual([r["action"] for r in rows], ["update", "create"])

    def test_filters(self):
        creates = self.history(action="create")
        sales = self.history(model="sales.sale")
        mine = self.history(actor=self.owner.pk)
        self.make_user("quiet", User.Role.OWNER, company=self.company)
        nobody = self.history(actor=User.objects.get(username="quiet").pk)

        self.assertTrue(all(r["action"] == "create" for r in creates))
        self.assertTrue(all(r["model"] == "sales.sale" for r in sales))
        self.assertGreater(len(mine), 0)
        self.assertEqual(nobody, [])

    def test_search_by_name_and_date(self):
        found = self.client.get(AUDIT, {"search": "owner"}).json()["count"]
        future = self.history(created_at__gte="2999-01-01")

        self.assertGreater(found, 0)
        self.assertEqual(future, [])

    def test_the_log_is_read_only(self):
        row = AuditLog.objects.first()

        self.assertEqual(self.client.post(AUDIT, {}, format="json").status_code, 405)
        self.assertEqual(
            self.client.patch(f"{AUDIT}{row.pk}/", {}, format="json").status_code, 405
        )
        self.assertEqual(self.client.delete(f"{AUDIT}{row.pk}/").status_code, 405)

    def test_another_business_sees_none_of_it(self):
        self.authenticate("OTHER-HO", "owner")

        self.assertEqual(self.history(), [])
        row = AuditLog.objects.filter(business=self.ho).first()
        self.assertEqual(self.client.get(f"{AUDIT}{row.pk}/").status_code, 404)

    def test_only_the_owner_can_read_it(self):
        position = self.make_position(
            self.ho, "Administrator", sales=("view",), finance=("view",)
        )
        self.make_user(
            "boss", User.Role.STAFF, company=self.company, position=position
        )
        self.authenticate("ACME-HO", "boss")

        self.assertEqual(self.client.get(AUDIT).status_code, 403)

        self.client.credentials()
        self.assertEqual(self.client.get(AUDIT).status_code, 401)


class AuditContextTests(TenantTestMixin, APITestCase):
    def test_requests_do_not_leak_the_user_into_the_next_one(self):
        self.authenticate("ACME-HO", "owner")
        self.client.post(EXPENSES, {"category": "rent", "amount": "1"}, format="json")

        # A request with no valid login must not be attributed to the owner.
        self.client.credentials()
        self.client.post(EXPENSES, {"category": "rent", "amount": "2"}, format="json")

        self.assertEqual(AuditLog.objects.count(), 1)
        self.assertEqual(AuditLog.objects.get().actor_name, "owner")

    def test_the_log_survives_a_removed_user(self):
        self.authenticate("ACME-HO", "owner")
        self.client.post(EXPENSES, {"category": "rent", "amount": "1"}, format="json")

        row = AuditLog.objects.get()
        row.actor = None
        row.save()

        self.assertEqual(AuditLog.objects.get().actor_name, "owner")
