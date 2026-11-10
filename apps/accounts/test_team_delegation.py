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

    def test_admin_creates_edits_resets_and_deactivates_staff(self):
        self.as_user("admin1")

        created = self.create()
        self.assertEqual(created.status_code, 201, created.data)
        pk = created.data["id"]
        self.assertIn("temporary_password", created.data)

        edited = self.client.patch(
            f"{STAFF_URL}{pk}/", {"phone": "9800000000"}, format="json"
        )
        self.assertEqual(edited.status_code, 200, edited.data)

        reset = self.client.post(f"{STAFF_URL}{pk}/reset-password/", {}, format="json")
        self.assertEqual(reset.status_code, 200, reset.data)
        self.assertIn("temporary_password", reset.data)

        gone = self.client.delete(f"{STAFF_URL}{pk}/")
        self.assertEqual(gone.status_code, 204)
        self.assertFalse(User.objects.get(pk=pk).is_active)

        back = self.client.post(f"{STAFF_URL}{pk}/reactivate/", {}, format="json")
        self.assertEqual(back.status_code, 200, back.data)

    def test_admin_lists_only_staff_within_their_own_level(self):
        self.as_user("admin1")

        usernames = {row["username"] for row in self.client.get(STAFF_URL).data["results"]}

        self.assertEqual(usernames, {"admin2", "cashier"})
        # Not themselves, not the finance lead, not the accountant account.
        self.assertNotIn("admin1", usernames)
        self.assertNotIn("boss", usernames)
        self.assertNotIn("acct", usernames)

    # ---- the limits

    def test_admin_cannot_assign_a_position_with_more_access(self):
        self.as_user("admin1")

        response = self.create(position=self.boss_pos.pk)

        self.assertEqual(response.status_code, 400)
        self.assertIn("position", response.data)

        promote = self.client.patch(
            f"{STAFF_URL}{self.cashier.pk}/",
            {"position": self.boss_pos.pk},
            format="json",
        )
        self.assertEqual(promote.status_code, 400)

    def test_admin_and_accountant_are_no_longer_roles(self):
        self.as_user("admin1")

        response = self.create(role="accountant", position=None)
        self.assertEqual(response.status_code, 400)
        self.assertIn("role", response.data)

        promote = self.client.patch(
            f"{STAFF_URL}{self.cashier.pk}/", {"role": "admin"}, format="json"
        )
        self.assertEqual(promote.status_code, 400)

    def test_admin_cannot_touch_more_powerful_staff_or_themselves(self):
        self.as_user("admin1")

        for target in (self.boss, self.admin, self.accountant):
            url = f"{STAFF_URL}{target.pk}/"
            self.assertEqual(self.client.get(url).status_code, 404, target.username)
            self.assertEqual(self.client.delete(url).status_code, 404, target.username)
            self.assertEqual(
                self.client.post(f"{url}reset-password/", {}, format="json").status_code,
                404,
                target.username,
            )

        self.assertTrue(User.objects.get(pk=self.boss.pk).is_active)

    def test_admin_can_read_but_not_change_positions(self):
        self.as_user("admin1")

        names = {p["name"] for p in self.client.get(POSITIONS_URL).data["results"]}
        # Only the ones they could assign.
        self.assertEqual(names, {"Administration", "Sales counter", "HR viewer"})

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
