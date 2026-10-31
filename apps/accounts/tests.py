
from rest_framework.test import APITestCase

from apps.accounts.models import Position, User
from apps.businesses.models import Business
from apps.companies.models import Company


class TenantLoginTests(APITestCase):

    def setUp(self):
        self.company = Company.objects.create(
            name="Test Company",
            code="TEST-COMPANY",
        )

        self.head_office = Business.objects.create(
            name="Test Head Office",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=self.company,
            business_code="TEST-HO",
        )

        self.branch = Business.objects.create(
            name="Test Branch",
            business_type=Business.BusinessType.BRANCH,
            company=self.company,
            parent_business=self.head_office,
            business_code="TEST-BRANCH",
        )

        self.login_url = "/api/v1/auth/login/"
        self.me_url = "/api/v1/auth/me/"
        self.refresh_url = "/api/v1/auth/token/refresh/"

    def create_user(self, username, password="TestPass123!", **kwargs):
        return User.objects.create_user(
            username=username,
            password=password,
            **kwargs,
        )

    def login(self, business_code, username, password="TestPass123!"):
        return self.client.post(
            self.login_url,
            {
                "business_code": business_code,
                "username": username,
                "password": password,
            },
            format="json",
        )

    def get_refresh_token(self, business_code, username):
        response = self.login(business_code, username)

        self.assertEqual(
            response.status_code,
            200,
            response.data,
        )

        return response.data["refresh"]

    def refresh(self, refresh_token):
        return self.client.post(
            self.refresh_url,
            {"refresh": refresh_token},
            format="json",
        )

    # --------------------------------------------------
    # LOGIN TESTS
    # --------------------------------------------------

    def test_company_owner_can_login_to_head_office(self):
        self.create_user(
            username="company-owner",
            role=User.Role.OWNER,
            company=self.company,
        )

        response = self.login("TEST-HO", "company-owner")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["active_business"]["access_mode"],
            "full",
        )

    def test_company_owner_gets_read_only_branch_access(self):
        self.create_user(
            username="company-owner",
            role=User.Role.OWNER,
            company=self.company,
        )

        response = self.login("TEST-BRANCH", "company-owner")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["active_business"]["access_mode"],
            "read_only",
        )

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {response.data['access']}"
        )

        me_response = self.client.get(self.me_url)

        self.assertEqual(me_response.status_code, 200)
        self.assertEqual(
            me_response.data["active_business"]["business_code"],
            "TEST-BRANCH",
        )
        self.assertEqual(
            me_response.data["active_business"]["access_mode"],
            "read_only",
        )

    def test_user_cannot_login_to_another_company(self):
        self.create_user(
            username="company-owner",
            role=User.Role.OWNER,
            company=self.company,
        )

        other_company = Company.objects.create(
            name="Other Test Company",
            code="OTHER-COMPANY",
        )

        Business.objects.create(
            name="Other Head Office",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=other_company,
            business_code="OTHER-HO",
        )

        response = self.login("OTHER-HO", "company-owner")

        self.assertEqual(response.status_code, 400)

    def test_company_staff_cannot_login_to_branch(self):
        position = Position.objects.create(
            business=self.head_office,
            name="Test Staff Position",
        )

        self.create_user(
            username="company-staff",
            role=User.Role.STAFF,
            company=self.company,
            position=position,
        )

        response = self.login("TEST-BRANCH", "company-staff")

        self.assertEqual(response.status_code, 400)

    def test_company_staff_can_login_to_head_office(self):
        position = Position.objects.create(
            business=self.head_office,
            name="Head Office Staff Position",
        )

        self.create_user(
            username="company-staff",
            role=User.Role.STAFF,
            company=self.company,
            position=position,
        )

        response = self.login("TEST-HO", "company-staff")

        self.assertEqual(response.status_code, 200)

    def test_staff_with_inactive_position_cannot_login(self):
        position = Position.objects.create(
            business=self.head_office,
            name="Inactive Staff Position",
            is_active=False,
        )

        self.create_user(
            username="inactive-staff",
            role=User.Role.STAFF,
            company=self.company,
            position=position,
        )

        response = self.login("TEST-HO", "inactive-staff")

        self.assertEqual(response.status_code, 400)

    def test_wrong_password_is_rejected(self):
        self.create_user(
            username="company-owner",
            role=User.Role.OWNER,
            company=self.company,
        )

        response = self.login(
            "TEST-HO",
            "company-owner",
            password="WrongPassword123!",
        )

        self.assertEqual(response.status_code, 400)

    def test_inactive_business_is_rejected(self):
        self.branch.is_active = False
        self.branch.save(update_fields=["is_active"])

        self.create_user(
            username="company-owner",
            role=User.Role.OWNER,
            company=self.company,
        )

        response = self.login("TEST-BRANCH", "company-owner")

        self.assertEqual(response.status_code, 400)

    def test_inactive_company_is_rejected(self):
        self.company.is_active = False
        self.company.save(update_fields=["is_active"])

        self.create_user(
            username="company-owner",
            role=User.Role.OWNER,
            company=self.company,
        )

        response = self.login("TEST-HO", "company-owner")

        self.assertEqual(response.status_code, 400)


class TenantRefreshSecurityTests(TenantLoginTests):

    def test_active_user_can_refresh_token(self):
        self.create_user(
            username="refresh-owner",
            role=User.Role.OWNER,
            company=self.company,
        )

        refresh_token = self.get_refresh_token(
            "TEST-HO",
            "refresh-owner",
        )

        response = self.refresh(refresh_token)

        self.assertEqual(
            response.status_code,
            200,
            response.data,
        )
        self.assertIn("access", response.data)

    def test_inactive_business_cannot_refresh_token(self):
        self.create_user(
            username="refresh-owner",
            role=User.Role.OWNER,
            company=self.company,
        )

        refresh_token = self.get_refresh_token(
            "TEST-BRANCH",
            "refresh-owner",
        )

        self.branch.is_active = False
        self.branch.save(update_fields=["is_active"])

        response = self.refresh(refresh_token)

        self.assertEqual(response.status_code, 401)

    def test_inactive_company_cannot_refresh_token(self):
        self.create_user(
            username="refresh-owner",
            role=User.Role.OWNER,
            company=self.company,
        )

        refresh_token = self.get_refresh_token(
            "TEST-HO",
            "refresh-owner",
        )

        self.company.is_active = False
        self.company.save(update_fields=["is_active"])

        response = self.refresh(refresh_token)

        self.assertEqual(response.status_code, 401)

    def test_inactive_staff_position_cannot_refresh_token(self):
        position = Position.objects.create(
            business=self.head_office,
            name="Refresh Staff Position",
        )

        self.create_user(
            username="refresh-staff",
            role=User.Role.STAFF,
            company=self.company,
            position=position,
        )

        refresh_token = self.get_refresh_token(
            "TEST-HO",
            "refresh-staff",
        )

        position.is_active = False
        position.save(update_fields=["is_active"])

        response = self.refresh(refresh_token)

        self.assertEqual(response.status_code, 401)

    def test_inactive_user_cannot_refresh_token(self):
        self.create_user(
            username="refresh-owner",
            role=User.Role.OWNER,
            company=self.company,
        )

        refresh_token = self.get_refresh_token(
            "TEST-HO",
            "refresh-owner",
        )

        user = User.objects.get(username="refresh-owner")
        user.is_active = False
        user.save(update_fields=["is_active"])

        response = self.refresh(refresh_token)

        self.assertEqual(response.status_code, 401)
