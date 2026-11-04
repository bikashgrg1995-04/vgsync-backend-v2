from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.businesses.models import Business
from apps.common.testing import TenantTestMixin

BUSINESSES = "/api/v1/businesses/"
COMPANY = "/api/v1/company/"


class BusinessManagementTests(TenantTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate("ACME-HO", "owner")

    def add(self, **extra):
        payload = {"name": "Acme Pokhara", "business_code": "acme-pkr", **extra}

        return self.client.post(BUSINESSES, payload, format="json")

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
        self.assertEqual(
            self.client.patch(f"{BUSINESSES}{other.pk}/", {"name": "x"}, format="json").status_code,
            404,
        )

    def test_a_branch_is_added_under_the_head_office(self):
        response = self.add(address="Lakeside", phone="061-123", email="pkr@acme.test")

        self.assertEqual(response.status_code, 201, response.data)
        data = response.json()
        self.assertEqual(data["business_type"], "branch")
        # Codes are kept in capitals.
        self.assertEqual(data["business_code"], "ACME-PKR")
        branch = Business.objects.get(pk=data["id"])
        self.assertEqual(branch.company, self.company)
        self.assertEqual(branch.parent_business, self.ho)
        self.assertEqual(branch.created_by, self.owner)
        self.assertTrue(branch.is_active)

    def test_the_type_company_and_parent_cannot_be_chosen(self):
        response = self.add(
            business_type="independent",
            company=self.other_company.pk,
            parent_business=self.branch.pk,
        )

        branch = Business.objects.get(pk=response.json()["id"])
        self.assertEqual(branch.business_type, "branch")
        self.assertEqual(branch.company, self.company)
        self.assertEqual(branch.parent_business, self.ho)

    def test_code_and_name_rules(self):
        for payload in (
            {"business_code": "acme-br"},   # taken, in another case
            {"business_code": "OTHER-HO"},  # taken by another company
            {"business_code": "a"},
            {"business_code": "bad code"},
            {"business_code": "-start"},
            {"name": "  "},
            {"name": ""},
        ):
            self.assertEqual(self.add(**payload).status_code, 400, payload)

        # Head Office, Branch and the other company's Head Office.
        self.assertEqual(Business.objects.count(), 3)

    def test_a_branch_can_be_edited_but_its_code_is_fixed(self):
        edited = self.client.patch(
            f"{BUSINESSES}{self.branch.pk}/",
            {"name": "Acme Kathmandu", "phone": "01-555"},
            format="json",
        )
        recoded = self.client.patch(
            f"{BUSINESSES}{self.branch.pk}/",
            {"business_code": "NEW-CODE"},
            format="json",
        )

        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.json()["name"], "Acme Kathmandu")
        self.assertEqual(recoded.status_code, 400)
        self.branch.refresh_from_db()
        self.assertEqual(self.branch.business_code, "ACME-BR")

    def test_the_head_office_can_be_edited(self):
        response = self.client.patch(
            f"{BUSINESSES}{self.ho.pk}/", {"address": "Bharatpur"}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["address"], "Bharatpur")

    def test_businesses_are_never_deleted_and_status_cannot_be_patched(self):
        self.assertEqual(
            self.client.delete(f"{BUSINESSES}{self.branch.pk}/").status_code, 405
        )

        self.client.patch(
            f"{BUSINESSES}{self.branch.pk}/", {"is_active": False}, format="json"
        )
        self.branch.refresh_from_db()
        self.assertTrue(self.branch.is_active)

    def test_a_branch_can_be_deactivated_and_activated(self):
        off = self.client.post(f"{BUSINESSES}{self.branch.pk}/deactivate/")
        again = self.client.post(f"{BUSINESSES}{self.branch.pk}/deactivate/")
        on = self.client.post(f"{BUSINESSES}{self.branch.pk}/activate/")
        twice = self.client.post(f"{BUSINESSES}{self.branch.pk}/activate/")

        self.assertEqual(off.status_code, 200)
        self.assertFalse(off.json()["is_active"])
        self.assertEqual(again.status_code, 409)
        self.assertEqual(on.status_code, 200)
        self.assertTrue(on.json()["is_active"])
        self.assertEqual(twice.status_code, 409)

    def test_the_head_office_cannot_be_deactivated(self):
        response = self.client.post(f"{BUSINESSES}{self.ho.pk}/deactivate/")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "not_a_branch")
        self.ho.refresh_from_db()
        self.assertTrue(self.ho.is_active)

    def test_a_deactivated_branch_can_no_longer_be_entered(self):
        self.make_user("clerk", User.Role.ADMIN, business=self.branch)
        self.assertEqual(self.login("ACME-BR", "clerk").status_code, 200)

        # Logging in cleared the owner's token; sign the owner in again.
        self.authenticate("ACME-HO", "owner")
        self.client.post(f"{BUSINESSES}{self.branch.pk}/deactivate/")

        self.assertEqual(self.login("ACME-BR", "clerk").status_code, 400)

    def test_filters_and_search(self):
        self.add()
        self.client.post(f"{BUSINESSES}{self.branch.pk}/deactivate/")

        branches = self.client.get(BUSINESSES, {"business_type": "branch"}).json()
        inactive = self.client.get(BUSINESSES, {"is_active": "false"}).json()
        found = self.client.get(BUSINESSES, {"search": "pokhara"}).json()

        self.assertEqual(branches["count"], 2)
        self.assertEqual([r["business_code"] for r in inactive["results"]], ["ACME-BR"])
        self.assertEqual(found["count"], 1)

    def test_a_branch_cannot_be_added_from_inside_a_branch(self):
        """A company owner is read-only in a branch, so writes are refused."""
        tokens = self.authenticate("ACME-HO", "owner")
        switched = self.client.post(
            "/api/v1/auth/business-context/",
            {"refresh": tokens["refresh"], "business_id": self.branch.pk},
            format="json",
        )
        self.assertEqual(switched.status_code, 200, switched.data)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {switched.json()['access']}"
        )

        self.assertEqual(self.client.get(BUSINESSES).status_code, 200)
        self.assertEqual(self.add(business_code="ZZ-NEW").status_code, 403)
        self.assertEqual(
            self.client.post(f"{BUSINESSES}{self.branch.pk}/deactivate/").status_code, 403
        )

    def test_only_the_owner_can_manage_businesses(self):
        self.make_user("boss", User.Role.ADMIN, company=self.company)
        self.authenticate("ACME-HO", "boss")

        self.assertEqual(self.client.get(BUSINESSES).status_code, 403)
        self.assertEqual(self.add().status_code, 403)

        self.client.credentials()
        self.assertEqual(self.client.get(BUSINESSES).status_code, 401)

    def test_the_branch_list_still_works(self):
        self.assertEqual(self.client.get(f"{BUSINESSES}branches/").status_code, 200)


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

    def test_the_owner_sees_and_edits_only_their_business(self):
        listed = self.client.get(BUSINESSES).json()

        self.assertEqual([r["business_code"] for r in listed["results"]], ["SOLO"])

        edited = self.client.patch(
            f"{BUSINESSES}{self.solo.pk}/", {"phone": "9800"}, format="json"
        )
        self.assertEqual(edited.status_code, 200)

    def test_an_independent_business_cannot_add_branches(self):
        response = self.client.post(
            BUSINESSES, {"name": "Branch", "business_code": "SOLO-B"}, format="json"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "not_head_office")

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

    def test_the_company_can_be_renamed_but_not_recoded_or_switched_off(self):
        response = self.client.patch(
            COMPANY,
            {"name": "Acme Group", "code": "NEW", "is_active": False},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.company.refresh_from_db()
        self.assertEqual(self.company.name, "Acme Group")
        self.assertEqual(self.company.code, "ACME")
        self.assertTrue(self.company.is_active)

    def test_a_blank_name_is_refused(self):
        self.assertEqual(
            self.client.patch(COMPANY, {"name": " "}, format="json").status_code, 400
        )

    def test_only_the_company_owner_can_use_it(self):
        self.make_user("boss", User.Role.ADMIN, company=self.company)
        self.authenticate("ACME-HO", "boss")

        self.assertEqual(self.client.get(COMPANY).status_code, 403)
        self.assertEqual(self.client.patch(COMPANY, {"name": "x"}, format="json").status_code, 403)

    def test_another_company_is_untouched(self):
        self.client.patch(COMPANY, {"name": "Renamed"}, format="json")
        self.other_company.refresh_from_db()

        self.assertEqual(self.other_company.name, "Other")
