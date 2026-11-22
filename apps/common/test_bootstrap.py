from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.common.bootstrap import BOOTSTRAP_PATHS
from apps.common.testing import TenantTestMixin

URL = "/api/v1/bootstrap/"


class BootstrapTests(TenantTestMixin, APITestCase):
    def test_requires_sign_in(self):
        self.assertEqual(self.client.get(URL).status_code, 401)

    def test_owner_gets_every_list_and_each_equals_its_own_endpoint(self):
        self.authenticate("ACME-HO", "owner")
        items = self.client.get(URL).data["items"]

        self.assertEqual(set(items), set(BOOTSTRAP_PATHS))
        for path in BOOTSTRAP_PATHS:
            alone = self.client.get("/api/v1" + path)
            self.assertEqual(alone.status_code, 200, path)
            self.assertEqual(items[path], alone.json(), path)

    def test_a_list_the_person_may_not_see_is_left_out(self):
        # A staff member who may only see sales: other modules' lists are not in the packet.
        position = self.make_position(self.ho, "Seller", sales=("view",))
        self.make_user(
            "seller", User.Role.STAFF, company=self.company,
            position=position,
        )
        self.authenticate("ACME-HO", "seller")

        items = self.client.get(URL).data["items"]

        self.assertIn("/sales/?page=1&page_size=100", items)
        self.assertNotIn("/finance/expenses/?page=1&page_size=100", items)
        self.assertNotIn("/staff/?page=1&page_size=100", items)

    def test_the_packet_is_kept_briefly_and_not_shared_between_people(self):
        self.authenticate("ACME-HO", "owner")
        first = self.client.get(URL).data["generated_at"]
        second = self.client.get(URL).data["generated_at"]
        self.assertEqual(first, second)  # served from the server cache
