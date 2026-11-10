from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.businesses.models import Business
from apps.common.testing import TenantTestMixin

BUSINESSES = "/api/v1/businesses/"
COMPANY = "/api/v1/company/"


class BusinessReadingTests(TenantTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate("ACME-HO", "owner")

    def test_a_company_owner_sees_the_head_office_and_all_branches(self):
        listed = self.client.get(BUSINESSES).json()

        self.assertEqual(listed["count"], 2)
        types = {row["business_code"]: row["business_type"] for row in listed["results"]}
        self.assertEqual(types, {"ACME-HO": "head_office", "ACME-BR": "branch"})

    def test_other_companies_are_never_shown(self):
        codes = [row["business_code"] for row in self.client.get(BUSINESSES).json()["results"]]

        self.assertNotIn("OTHER-HO", codes)
        other = Business.objects.get(business_code="OTHER-HO")
        self.assertEqual(self.client.get(f"{BUSINESSES}{other.pk}/").status_code, 404)

    def test_filters_and_search(self):
        Business.objects.filter(pk=self.branch.pk).update(is_active=False)

        branches = self.client.get(BUSINESSES, {"business_type": "branch"}).json()
        inactive = self.client.get(BUSINESSES, {"is_active": "false"}).json()
        found = self.client.get(BUSINESSES, {"search": "acme-br"}).json()

        self.assertEqual(branches["count"], 1)
        self.assertEqual([r["business_code"] for r in inactive["results"]], ["ACME-BR"])
        self.assertEqual(found["count"], 1)

    def test_only_the_owner_can_read_businesses(self):
        self.make_user(
            "boss", User.Role.STAFF, company=self.company,
            position=self.make_position(self.ho, "Boss", staff=("view",)),
        )
        self.authenticate("ACME-HO", "boss")

        self.assertEqual(self.client.get(BUSINESSES).status_code, 403)

        self.client.credentials()
        self.assertEqual(self.client.get(BUSINESSES).status_code, 401)

    def test_the_branch_list_still_works(self):
        self.assertEqual(self.client.get(f"{BUSINESSES}branches/").status_code, 200)

    def test_a_switched_off_branch_can_no_longer_be_entered(self):
        self.make_user(
            "clerk", User.Role.STAFF, business=self.branch,
            position=self.make_position(self.branch, "Clerk", sales=("view",)),
        )
        self.assertEqual(self.login("ACME-BR", "clerk").status_code, 200)

        Business.objects.filter(pk=self.branch.pk).update(is_active=False)

        self.assertEqual(self.login("ACME-BR", "clerk").status_code, 400)

    def test_a_switched_off_company_can_no_longer_sign_in(self):
        self.company.is_active = False
        self.company.save()

        self.assertEqual(self.login("ACME-HO", "owner").status_code, 400)


class OwnersCannotChangeBusinessesTests(TenantTestMixin, APITestCase):
    """Only a super admin, in Django Admin, creates, edits or switches off a business."""

    def setUp(self):
        super().setUp()
        self.authenticate("ACME-HO", "owner")

    def test_the_owner_cannot_add_edit_or_switch_off(self):
        url = f"{BUSINESSES}{self.branch.pk}/"

        self.assertEqual(
            self.client.post(BUSINESSES, {"name": "X", "business_code": "X-1"}, format="json").status_code,
            405,
        )
        self.assertEqual(self.client.patch(url, {"name": "New"}, format="json").status_code, 405)
        self.assertEqual(self.client.delete(url).status_code, 405)
        self.assertEqual(self.client.post(f"{url}deactivate/").status_code, 404)
        self.assertEqual(self.client.post(f"{url}activate/").status_code, 404)

        self.branch.refresh_from_db()
        self.assertTrue(self.branch.is_active)
        self.assertNotEqual(self.branch.name, "New")

    def test_the_owner_cannot_rename_the_company(self):
        response = self.client.patch(COMPANY, {"name": "Renamed"}, format="json")

        self.assertEqual(response.status_code, 405)
        self.company.refresh_from_db()
        self.assertEqual(self.company.name, "Acme")


class IndependentBusinessTests(TenantTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.solo = Business.objects.create(
            name="Solo Motors",
            business_type=Business.BusinessType.INDEPENDENT,
            business_code="SOLO",
        )
        self.make_user("soloboss", User.Role.OWNER, business=self.solo)
        self.authenticate("SOLO", "soloboss")

    def test_the_owner_sees_only_their_business_and_cannot_edit_it(self):
        listed = self.client.get(BUSINESSES).json()

        self.assertEqual([r["business_code"] for r in listed["results"]], ["SOLO"])
        self.assertEqual(
            self.client.patch(f"{BUSINESSES}{self.solo.pk}/", {"phone": "9800"}, format="json").status_code,
            405,
        )

    def test_it_has_no_company(self):
        response = self.client.get(COMPANY)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "no_company")


class CompanyTests(TenantTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate("ACME-HO", "owner")

    def test_the_owner_sees_their_company(self):
        data = self.client.get(COMPANY).json()

        self.assertEqual(data["name"], "Acme")
        self.assertEqual(data["code"], "ACME")
        self.assertTrue(data["is_active"])

    def test_only_the_company_owner_can_use_it(self):
        self.make_user(
            "boss", User.Role.STAFF, company=self.company,
            position=self.make_position(self.ho, "Boss", staff=("view",)),
        )
        self.authenticate("ACME-HO", "boss")

        self.assertEqual(self.client.get(COMPANY).status_code, 403)
