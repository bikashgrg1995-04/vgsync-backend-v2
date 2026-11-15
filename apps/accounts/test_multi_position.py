"""One person, more than one position: access is the union."""

from rest_framework.test import APITestCase

from apps.accounts import rbac
from apps.accounts.models import User
from apps.common.testing import TenantTestMixin

STAFF = "/api/v1/staff/"
SALES = "/api/v1/sales/"
PURCHASES = "/api/v1/purchases/"


class MultiPositionTests(TenantTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.sales_pos = self.make_position(self.ho, "Seller", sales=("view",))
        self.buy_pos = self.make_position(self.ho, "Buyer", purchases=("view",))
        self.third = self.make_position(self.ho, "Third", inventory=("view",))
        self.person = self.make_user(
            "multi", User.Role.STAFF, company=self.company, position=self.sales_pos
        )

    def give(self, ids):
        self.authenticate("ACME-HO", "owner")

        return self.client.patch(
            f"{STAFF}{self.person.pk}/", {"extra_positions": ids}, format="json"
        )

    def test_access_is_the_union(self):
        self.authenticate("ACME-HO", "multi")
        self.assertEqual(self.client.get(PURCHASES + "purchases/").status_code, 403)

        response = self.give([self.buy_pos.pk])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["extra_positions"], [self.buy_pos.pk])
        self.assertEqual(response.data["extra_position_names"], ["Buyer"])

        self.authenticate("ACME-HO", "multi")
        self.assertIn("purchases.view", rbac.effective_permissions(User.objects.get(pk=self.person.pk)))
        self.assertIn("sales.view", rbac.effective_permissions(User.objects.get(pk=self.person.pk)))

    def test_the_main_position_cannot_be_repeated(self):
        response = self.give([self.sales_pos.pk])

        self.assertEqual(response.status_code, 400)

    def test_an_inactive_extra_position_gives_nothing(self):
        self.give([self.buy_pos.pk])
        self.buy_pos.is_active = False
        self.buy_pos.save(update_fields=["is_active"])

        codes = rbac.effective_permissions(User.objects.get(pk=self.person.pk))
        self.assertNotIn("purchases.view", codes)

    def test_removing_extras_removes_their_access(self):
        self.give([self.buy_pos.pk, self.third.pk])
        self.give([])

        codes = rbac.effective_permissions(User.objects.get(pk=self.person.pk))
        self.assertNotIn("purchases.view", codes)
        self.assertNotIn("inventory.view", codes)

    def test_a_position_with_extra_holders_cannot_be_deleted(self):
        self.give([self.buy_pos.pk])
        response = self.client.delete(f"/api/v1/positions/{self.buy_pos.pk}/")

        self.assertEqual(response.status_code, 409)

    def test_position_staff_count_includes_extra_holders(self):
        self.give([self.buy_pos.pk])
        rows = self.client.get("/api/v1/positions/").json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        buyer = next(r for r in rows if r["id"] == self.buy_pos.pk)

        self.assertEqual(buyer["staff_count"], 1)

    def test_it_can_be_given_when_creating(self):
        self.authenticate("ACME-HO", "owner")
        response = self.client.post(
            STAFF,
            {
                "first_name": "New", "last_name": "Hire", "username": "newhire",
                "role": "staff", "position": self.sales_pos.pk,
                "extra_positions": [self.buy_pos.pk, self.third.pk],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(response.data["extra_positions"]), 2)

    def test_a_delegated_admin_cannot_hand_out_a_position_above_them(self):
        adm_pos = self.make_position(self.ho, "Admin")
        rbac.write_position_codes(adm_pos, {"staff.view", "staff.assign_position", "staff.update", "sales.view"})
        self.make_user("adm", User.Role.STAFF, company=self.company, position=adm_pos)
        self.authenticate("ACME-HO", "adm")
        response = self.client.patch(
            f"{STAFF}{self.person.pk}/", {"extra_positions": [self.buy_pos.pk]}, format="json"
        )

        self.assertIn(response.status_code, (400, 404), response.data)
