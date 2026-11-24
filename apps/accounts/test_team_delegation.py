"""
Staff whose Position grants the "staff" module (the "Administration"
position) can manage other staff, within limits.
"""

from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.common.testing import TenantTestMixin

STAFF_URL = "/api/v1/staff/"
POSITIONS_URL = "/api/v1/positions/"


class TeamDelegationTests(TenantTestMixin, APITestCase):

    def setUp(self):
        super().setUp()

        full = ("view", "create", "update", "delete")

        self.admin_pos = self.make_position(
            self.ho, "Administration", staff=full, sales=("view", "create")
        )
        self.sales_pos = self.make_position(
            self.ho, "Sales counter", sales=("view",)
        )
        # Grants finance, which the Administration position does not have.
        self.boss_pos = self.make_position(
            self.ho, "Finance lead", staff=full, finance=("view",)
        )
        self.viewer_pos = self.make_position(
            self.ho, "HR viewer", staff=("view",)
        )

        self.admin = self.make_user(
            "admin1", User.Role.STAFF, company=self.company,
            position=self.admin_pos,
        )
        self.other_admin = self.make_user(
            "admin2", User.Role.STAFF, company=self.company,
            position=self.admin_pos,
        )
        self.boss = self.make_user(
            "boss", User.Role.STAFF, company=self.company,
            position=self.boss_pos,
        )
        self.cashier = self.make_user(
            "cashier", User.Role.STAFF, company=self.company,
            position=self.sales_pos,
        )
        # Accounts staff: finance only, and no access to the team API.
        self.acct_pos = self.make_position(
            self.ho, "Accounts", finance=("view",)
        )
        self.accountant = self.make_user(
            "acct", User.Role.STAFF, company=self.company,
            position=self.acct_pos,
        )

    def as_user(self, username):
        self.authenticate("ACME-HO", username)

    def create(self, **overrides):
        payload = {
            "username": "newbie",
            "first_name": "New",
            "role": "staff",
            "position": self.sales_pos.pk,
        }
        payload.update(overrides)
        return self.client.post(STAFF_URL, payload, format="json")

    # ---- what the profile tells the screens

    def test_profile_says_who_can_manage_the_team(self):
        owner = self.authenticate("ACME-HO", "owner")
        self.assertTrue(owner["user"]["can_manage_team"])

        admin = self.authenticate("ACME-HO", "admin1")
        self.assertTrue(admin["user"]["can_manage_team"])
        modules = {p["module"] for p in admin["user"]["permissions"]}
        self.assertEqual(modules, {"staff", "sales"})

        cashier = self.authenticate("ACME-HO", "cashier")
        self.assertFalse(cashier["user"]["can_manage_team"])

        acct = self.authenticate("ACME-HO", "acct")
        self.assertFalse(acct["user"]["can_manage_team"])
        self.assertEqual(
            {p["module"] for p in acct["user"]["permissions"]}, {"finance"}
        )

    # ---- who can use the API at all

    def test_staff_without_the_staff_module_cannot_use_it(self):
        self.as_user("cashier")
        self.assertEqual(self.client.get(STAFF_URL).status_code, 403)
        self.assertEqual(self.client.get(POSITIONS_URL).status_code, 403)

    def test_a_position_without_the_staff_module_is_not_enough(self):
        self.as_user("acct")
        self.assertEqual(self.client.get(STAFF_URL).status_code, 403)

    def test_view_only_position_can_read_but_not_change(self):
        viewer = self.make_user(
            "viewer", User.Role.STAFF, company=self.company,
            position=self.viewer_pos,
        )
        self.as_user("viewer")

        self.assertEqual(self.client.get(STAFF_URL).status_code, 200)
        self.assertEqual(self.create().status_code, 403)
        response = self.client.patch(
            f"{STAFF_URL}{self.cashier.pk}/", {"first_name": "X"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(viewer.is_active)

    # ---- what a delegated administrator can do

    def test_admin_can_read_staff_but_only_the_owner_changes_them(self):
        self.as_user("admin1")

        self.assertEqual(self.client.get(STAFF_URL).status_code, 200)

        # Creating, editing, resetting, deactivating and reactivating are the owner's alone.
        self.assertEqual(self.create().status_code, 403)
        url = f"{STAFF_URL}{self.cashier.pk}/"
        self.assertEqual(self.client.patch(url, {"phone": "9800000000"}, format="json").status_code, 403)
        self.assertEqual(self.client.post(f"{url}reset-password/", {}, format="json").status_code, 403)
        self.assertEqual(self.client.delete(url).status_code, 403)
        self.assertEqual(self.client.post(f"{url}reactivate/", {}, format="json").status_code, 403)
        self.assertTrue(User.objects.get(pk=self.cashier.pk).is_active)

    def test_owner_creates_edits_resets_and_deactivates_staff(self):
        self.as_user("owner")

        created = self.create()
        self.assertEqual(created.status_code, 201, created.data)
        pk = created.data["id"]
        self.assertIn("temporary_password", created.data)

        edited = self.client.patch(f"{STAFF_URL}{pk}/", {"phone": "9800000000"}, format="json")
        self.assertEqual(edited.status_code, 200, edited.data)

        reset = self.client.post(f"{STAFF_URL}{pk}/reset-password/", {}, format="json")
        self.assertEqual(reset.status_code, 200, reset.data)

        self.assertEqual(self.client.delete(f"{STAFF_URL}{pk}/").status_code, 204)
        self.assertFalse(User.objects.get(pk=pk).is_active)
        self.assertEqual(self.client.post(f"{STAFF_URL}{pk}/reactivate/", {}, format="json").status_code, 200)

    def test_admin_sees_everyone_on_the_team(self):
        self.as_user("admin1")

        rows = self.client.get(STAFF_URL).data["results"]
        usernames = {row["username"] for row in rows}

        self.assertEqual(
            usernames, {"admin1", "admin2", "boss", "cashier", "acct"}
        )
        # The directory shows who holds which position, nothing private.
        boss = next(r for r in rows if r["username"] == "boss")
        self.assertEqual(boss["position_name"], "Finance lead")
        self.assertNotIn("salary", " ".join(boss.keys()))

    def test_admin_sees_every_position_with_its_members(self):
        self.as_user("admin1")
        data = self.client.get(POSITIONS_URL).data
        by_name = {row["name"]: row for row in data["results"]}

        self.assertIn("Finance lead", by_name)
        self.assertEqual(data["hidden_count"], 0)
        self.assertEqual(
            {m["username"] for m in by_name["Administration"]["members"]},
            {"admin1", "admin2"},
        )
        self.assertEqual(
            [m["username"] for m in by_name["Sales counter"]["members"]],
            ["cashier"],
        )

    def test_admin_does_not_see_what_a_position_grants(self):
        self.as_user("admin1")
        row = self.client.get(f"{POSITIONS_URL}{self.boss_pos.pk}/").data

        self.assertNotIn("permissions", row)
        self.assertNotIn("granted", row)
        self.assertEqual(self.client.get(f"{POSITIONS_URL}catalogue/").status_code, 403)
        self.assertEqual(self.client.get(f"{POSITIONS_URL}presets/").status_code, 403)

    def test_owner_sees_what_a_position_grants_and_its_members(self):
        self.as_user("owner")
        row = self.client.get(f"{POSITIONS_URL}{self.boss_pos.pk}/").data

        self.assertIn("finance.view", row["granted"])
        self.assertTrue(row["permissions"])
        self.assertEqual([m["username"] for m in row["members"]], ["boss"])

    def test_admin_cannot_open_access_or_documents_of_anyone(self):
        self.as_user("admin1")

        for suffix in ("access/", "documents/"):
            url = f"{STAFF_URL}{self.cashier.pk}/{suffix}"
            self.assertEqual(self.client.get(url).status_code, 403, suffix)

        self.as_user("owner")
        self.assertEqual(
            self.client.get(f"{STAFF_URL}{self.cashier.pk}/access/").status_code, 200
        )
        self.assertEqual(
            self.client.get(f"{STAFF_URL}{self.cashier.pk}/documents/").status_code, 200
        )

    def test_owner_sees_every_position_and_none_hidden(self):
        self.as_user("owner")
        data = self.client.get("/api/v1/positions/").data

        self.assertEqual(data["hidden_count"], 0)

    # ---- the limits

    def test_admin_cannot_assign_a_position_at_all(self):
        self.as_user("admin1")

        self.assertEqual(self.create(position=self.boss_pos.pk).status_code, 403)
        promote = self.client.patch(
            f"{STAFF_URL}{self.cashier.pk}/", {"position": self.boss_pos.pk}, format="json"
        )
        self.assertEqual(promote.status_code, 403)

    def test_admin_and_accountant_are_no_longer_roles(self):
        self.as_user("owner")

        response = self.create(role="accountant", position=None)
        self.assertEqual(response.status_code, 400)
        self.assertIn("role", response.data)

        promote = self.client.patch(
            f"{STAFF_URL}{self.cashier.pk}/", {"role": "admin"}, format="json"
        )
        self.assertEqual(promote.status_code, 400)

    def test_admin_cannot_change_or_deactivate_anyone(self):
        self.as_user("admin1")

        for target in (self.boss, self.admin, self.accountant, self.cashier):
            url = f"{STAFF_URL}{target.pk}/"
            self.assertEqual(self.client.get(url).status_code, 200, target.username)
            self.assertEqual(self.client.delete(url).status_code, 403, target.username)

        self.assertTrue(User.objects.get(pk=self.boss.pk).is_active)

    def test_admin_can_read_but_not_change_positions(self):
        self.as_user("admin1")

        names = {p["name"] for p in self.client.get(POSITIONS_URL).data["results"]}
        self.assertEqual(
            names,
            {"Administration", "Sales counter", "HR viewer", "Finance lead", "Accounts"},
        )

        created = self.client.post(POSITIONS_URL, {"name": "Sneaky"}, format="json")
        self.assertEqual(created.status_code, 403)

        changed = self.client.patch(
            f"{POSITIONS_URL}{self.admin_pos.pk}/",
            {"permissions": [{"module": "finance", "can_view": True}]},
            format="json",
        )
        self.assertEqual(changed.status_code, 403)

    def test_owner_still_manages_everyone_and_positions(self):
        self.authenticate("ACME-HO", "owner")

        usernames = {r["username"] for r in self.client.get(STAFF_URL).data["results"]}
        self.assertTrue({"admin1", "boss", "cashier", "acct"} <= usernames)

        created = self.client.post(POSITIONS_URL, {"name": "Night shift"}, format="json")
        self.assertEqual(created.status_code, 201, created.data)

    def test_other_companies_staff_are_invisible(self):
        other_pos = self.make_position(self.other_ho, "Administration", staff=("view",))
        self.make_user("stranger", User.Role.STAFF, company=self.other_company, position=other_pos)
        self.as_user("admin1")

        usernames = {r["username"] for r in self.client.get(STAFF_URL).data["results"]}
        self.assertNotIn("stranger", usernames)
