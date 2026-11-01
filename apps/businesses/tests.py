
from django.core.cache import cache
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.businesses.models import Business
from apps.companies.models import Company


PASSWORD = "TestPass123!"


class BranchListAPITests(APITestCase):

    def setUp(self):
        cache.clear()

        self.company = Company.objects.create(
            name="Acme",
            code="ACME",
        )
        self.other_company = Company.objects.create(
            name="Other Company",
            code="OTHER",
        )

        self.head_office = Business.objects.create(
            name="Acme HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=self.company,
            business_code="ACME-HO",
        )

        self.branch = Business.objects.create(
            name="Acme Branch",
            business_type=Business.BusinessType.BRANCH,
            company=self.company,
            parent_business=self.head_office,
            business_code="ACME-BR",
        )

        self.second_branch = Business.objects.create(
            name="Acme Branch 2",
            business_type=Business.BusinessType.BRANCH,
            company=self.company,
            parent_business=self.head_office,
            business_code="ACME-BR2",
        )

        self.other_head_office = Business.objects.create(
            name="Other HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=self.other_company,
            business_code="OTHER-HO",
        )

        self.other_branch = Business.objects.create(
            name="Other Branch",
            business_type=Business.BusinessType.BRANCH,
            company=self.other_company,
            parent_business=self.other_head_office,
            business_code="OTHER-BR",
        )

        self.login_url = "/api/v1/auth/login/"
        self.branch_list_url = "/api/v1/businesses/branches/"

    def create_user(self, username, **kwargs):
        return User.objects.create_user(
            username=username,
            password=PASSWORD,
            **kwargs,
        )

    def login(self, username, business_code="ACME-HO"):
        response = self.client.post(
            self.login_url,
            {
                "business_code": business_code,
                "username": username,
                "password": PASSWORD,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {response.data['access']}"
        )

        return response

    def test_company_owner_can_list_own_branches(self):
        self.create_user(
            "owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        self.login("owner")

        response = self.client.get(self.branch_list_url)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 2)

        branch_ids = {item["id"] for item in response.data}

        self.assertEqual(
            branch_ids,
            {self.branch.id, self.second_branch.id},
        )

    def test_company_admin_can_list_own_branches(self):
        self.create_user(
            "admin",
            role=User.Role.ADMIN,
            company=self.company,
        )
        self.login("admin")

        response = self.client.get(self.branch_list_url)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 2)

    def test_accountant_can_list_own_branches(self):
        self.create_user(
            "accountant",
            role=User.Role.ACCOUNTANT,
            company=self.company,
        )
        self.login("accountant")

        response = self.client.get(self.branch_list_url)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 2)

    def test_branch_user_can_list_company_branches(self):
        self.create_user(
            "owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        self.login("owner", "ACME-BR")

        response = self.client.get(self.branch_list_url)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 2)

    def test_other_company_branches_are_not_exposed(self):
        self.create_user(
            "owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        self.login("owner")

        response = self.client.get(self.branch_list_url)

        returned_ids = {item["id"] for item in response.data}

        self.assertNotIn(self.other_branch.id, returned_ids)

    def test_company_staff_cannot_list_branches(self):
        from apps.accounts.models import Position

        position = Position.objects.create(
            business=self.head_office,
            name="HO Staff",
        )

        self.create_user(
            "staff",
            role=User.Role.STAFF,
            company=self.company,
            position=position,
        )
        self.login("staff")

        response = self.client.get(self.branch_list_url)

        self.assertEqual(response.status_code, 403)

    def test_independent_business_user_cannot_list_branches(self):
        independent = Business.objects.create(
            name="Solo Shop",
            business_type=Business.BusinessType.INDEPENDENT,
            business_code="SOLO",
        )

        self.create_user(
            "solo-owner",
            role=User.Role.OWNER,
            business=independent,
        )

        response = self.client.post(
            self.login_url,
            {
                "business_code": "SOLO",
                "username": "solo-owner",
                "password": PASSWORD,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {response.data['access']}"
        )

        response = self.client.get(self.branch_list_url)

        self.assertEqual(response.status_code, 403)

    def test_inactive_branches_are_not_listed(self):
        self.second_branch.is_active = False
        self.second_branch.save(update_fields=["is_active"])

        self.create_user(
            "owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        self.login("owner")

        response = self.client.get(self.branch_list_url)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], self.branch.id)

    def test_branches_under_inactive_head_office_are_not_listed(self):
        self.head_office.is_active = False
        self.head_office.save(update_fields=["is_active"])

        # Login using the branch context, which remains independently active.
        self.create_user(
            "owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        self.login("owner", "ACME-BR")

        response = self.client.get(self.branch_list_url)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, [])

    def test_unauthenticated_user_cannot_list_branches(self):
        response = self.client.get(self.branch_list_url)

        self.assertEqual(response.status_code, 401)
