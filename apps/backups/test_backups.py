import io
import json
import zipfile

from apps.accounts.models import User
from apps.backups.models import BackupLog
from apps.sales.models import Customer
from apps.sales.test_sale_returns import ReturnTestCase

BACKUPS = "/api/v1/backups/"


def read(response):
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    return archive, json.loads(archive.read("manifest.json"))


class BackupTests(ReturnTestCase):
    def test_owner_gets_a_zip_with_a_manifest_and_one_csv_per_table(self):
        response = self.client.post(BACKUPS)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertIn("attachment", response["Content-Disposition"])

        archive, manifest = read(response)
        names = archive.namelist()

        self.assertIn("sales.Customer.csv", names)
        self.assertEqual(manifest["format_version"], 1)
        self.assertTrue(any(t["table"] == "sales.Customer" for t in manifest["tables"]))

    def test_passwords_are_never_included(self):
        archive, _ = read(self.client.post(BACKUPS))
        users = archive.read("accounts.User.csv").decode()

        self.assertNotIn("password", users.splitlines()[0])
        self.assertNotIn("pbkdf2", users)

    def test_company_owner_backup_covers_every_branch(self):
        Customer.objects.create(business=self.branch, name="Branch customer")

        archive, manifest = read(self.client.post(BACKUPS))

        self.assertEqual(manifest["scope"], "company")
        self.assertEqual(
            {b["code"] for b in manifest["businesses"]}, {"ACME-HO", "ACME-BR"}
        )
        self.assertIn("Branch customer", archive.read("sales.Customer.csv").decode())

    def test_the_backup_is_recorded(self):
        self.client.post(BACKUPS)

        log = BackupLog.objects.get()
        listed = self.client.get(BACKUPS).json()["results"]

        self.assertEqual(log.business, self.ho)
        self.assertEqual(log.created_by, self.owner)
        self.assertGreater(log.size_bytes, 0)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["created_by_name"], "owner")

    def test_staff_cannot_back_up(self):
        position = self.make_position(self.ho, "Viewer", sales=("view",))
        self.make_user(
            "viewer", User.Role.STAFF, company=self.company, position=position
        )
        self.authenticate("ACME-HO", "viewer")

        self.assertEqual(self.client.post(BACKUPS).status_code, 403)
        self.assertEqual(self.client.get(BACKUPS).status_code, 403)

    def test_only_this_business_is_in_a_branch_owner_backup(self):
        Customer.objects.create(business=self.ho, name="Head Office customer")
        Customer.objects.create(business=self.branch, name="Branch customer")
        self.make_user("branchowner", User.Role.OWNER, business=self.branch)
        self.authenticate("ACME-BR", "branchowner")

        archive, manifest = read(self.client.post(BACKUPS))
        customers = archive.read("sales.Customer.csv").decode()

        self.assertEqual([b["id"] for b in manifest["businesses"]], [self.branch.id])
        self.assertIn("Branch customer", customers)
        self.assertNotIn("Head Office customer", customers)


class RestoreTests(ReturnTestCase):
    def take(self):
        return self.client.post(BACKUPS).content

    def upload(self, data, path="restore/", confirm="ACME-HO", name="b.zip"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        body = {"file": SimpleUploadedFile(name, data, "application/zip")}

        if confirm is not None:
            body["confirm"] = confirm

        return self.client.post(BACKUPS + path, body, format="multipart")

    def test_restore_brings_back_records_and_undoes_later_changes(self):
        from apps.sales.models import Sale, SaleItem

        sale = self.paid_sale()
        data = self.take()
        sales_then = Sale.objects.count()
        items_then = SaleItem.objects.count()
        Customer.objects.filter(pk=self.customer.pk).update(name="Changed later")
        Customer.objects.create(business=self.ho, name="Made after backup")
        SaleItem.objects.filter(sale_id=sale["id"]).delete()

        response = self.upload(data)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertGreater(response.json()["restored"], 0)
        self.assertEqual(Sale.objects.count(), sales_then)
        self.assertEqual(SaleItem.objects.count(), items_then)  # lines are back
        self.assertFalse(Customer.objects.filter(name="Made after backup").exists())
        self.assertNotEqual(Customer.objects.get(pk=self.customer.pk).name, "Changed later")

    def test_new_records_after_a_restore_get_fresh_ids(self):
        data = self.take()
        self.upload(data)

        fresh = Customer.objects.create(business=self.ho, name="Fresh")

        self.assertGreater(fresh.pk, max(Customer.objects.exclude(pk=fresh.pk).values_list("pk", flat=True)))

    def test_a_restore_needs_the_business_code_typed(self):
        data = self.take()

        self.assertEqual(self.upload(data, confirm=None).status_code, 400)
        self.assertEqual(self.upload(data, confirm="nope").status_code, 400)

    def test_accounts_and_permissions_are_left_alone(self):
        data = self.take()
        self.make_user("later", User.Role.STAFF, company=self.company)

        self.assertEqual(self.upload(data).status_code, 200)
        self.assertTrue(User.objects.filter(username="later").exists())

    def test_a_backup_from_another_business_is_refused(self):
        self.make_user("branchowner", User.Role.OWNER, business=self.branch)
        self.authenticate("ACME-BR", "branchowner")
        branch_backup = self.take()
        self.authenticate("ACME-HO", "owner")

        response = self.upload(branch_backup)

        self.assertEqual(response.status_code, 400)
        self.assertIn("different business", response.json()["detail"])

    def test_garbage_is_refused_and_nothing_changes(self):
        before = Customer.objects.count()

        self.assertEqual(self.upload(b"not a zip").status_code, 400)
        self.assertEqual(Customer.objects.count(), before)

    def test_preview_changes_nothing_and_counts_rows(self):
        data = self.take()
        Customer.objects.create(business=self.ho, name="Extra")
        before = Customer.objects.filter(business__in=[self.ho, self.branch]).count()

        response = self.upload(data, path="restore-preview/", confirm=None)

        self.assertEqual(response.status_code, 200, response.content)
        row = next(t for t in response.json()["tables"] if t["table"] == "sales.Customer")
        self.assertEqual(row["now"], before)
        self.assertEqual(row["backup"], before - 1)
        self.assertEqual(
            Customer.objects.filter(business__in=[self.ho, self.branch]).count(), before
        )

    def test_a_safety_copy_is_kept_and_can_be_downloaded(self):
        data = self.take()
        Customer.objects.create(business=self.ho, name="Made after backup")

        self.upload(data)

        listed = self.client.get(BACKUPS).json()["results"]
        safety = next(r for r in listed if r["kind"] == "safety")
        self.assertTrue(safety["has_file"])
        copy = self.client.get(f"{BACKUPS}{safety['id']}/download/")
        self.assertEqual(copy.status_code, 200)

        # Restoring the safety copy brings the newer customer back.
        self.upload(copy.content)
        self.assertTrue(Customer.objects.filter(name="Made after backup").exists())

    def test_staff_need_the_backup_permissions(self):
        data = self.take()
        plain = self.make_position(self.ho, "Plain", sales=("view",))
        self.make_user("plain", User.Role.STAFF, company=self.company, position=plain)
        admin = self.make_position(self.ho, "Admin", backup=("view", "create"))
        self.make_user("admin", User.Role.STAFF, company=self.company, position=admin)

        self.authenticate("ACME-HO", "plain")
        self.assertEqual(self.client.post(BACKUPS).status_code, 403)

        self.authenticate("ACME-HO", "admin")
        self.assertEqual(self.client.post(BACKUPS).status_code, 200)
        self.assertEqual(self.client.get(BACKUPS).status_code, 200)
        self.assertEqual(self.upload(data).status_code, 403)

    def test_the_administrator_preset_includes_backup_and_restore(self):
        from apps.accounts.rbac import preset_codes

        codes = preset_codes("administrator")

        self.assertTrue({"backup.view", "backup.create", "backup.restore"} <= codes)
