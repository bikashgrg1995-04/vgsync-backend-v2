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
