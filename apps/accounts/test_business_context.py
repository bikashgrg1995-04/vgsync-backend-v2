from django.core.cache import cache
from rest_framework.test import APITestCase

from apps.accounts.models import Position, User
from apps.businesses.models import Business
from apps.companies.models import Company


PASSWORD = "TestPass123!"


class BusinessContextSwitchTests(APITestCase):

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
        self.other_head_office = Business.objects.create(
            name="Other HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=self.other_company,
            business_code="OTHER-HO",
        )

        self.switch_url = "/api/v1/auth/business-context/"
        self.login_url = "/api/v1/auth/login/"
        self.refresh_url = "/api/v1/auth/token/refresh/"
        self.me_url = "/api/v1/auth/me/"

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
        return response.data

    def switch(self, access, refresh, business_id):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {access}"
        )

        response = self.client.post(
            self.switch_url,
            {
                "refresh": refresh,
                "business_id": business_id,
            },
            format="json",
        )

        self.client.credentials()
        return response

    # --------------------------------------------------
    # SUCCESSFUL CONTEXT SWITCHING
    # --------------------------------------------------

    def test_company_owner_can_switch_head_office_to_branch(self):
        self.create_user(
            "owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        session = self.login("owner")

        response = self.switch(
            session["access"],
            session["refresh"],
            self.branch.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["active_business"]["id"],
            self.branch.id,
        )
        self.assertEqual(
            response.data["active_business"]["access_mode"],
            "read_only",
        )

    def test_company_owner_can_switch_branch_to_head_office(self):
        self.create_user(
            "owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        session = self.login("owner", "ACME-BR")

        response = self.switch(
            session["access"],
            session["refresh"],
            self.head_office.id,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["active_business"]["id"],
            self.head_office.id,
        )
        self.assertEqual(
            response.data["active_business"]["access_mode"],
            "full",
        )

    def test_new_access_token_uses_the_selected_business(self):
        self.create_user(
            "owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        session = self.login("owner")

        response = self.switch(
            session["access"],
            session["refresh"],
            self.branch.id,
        )

        self.assertEqual(response.status_code, 200, response.data)

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {response.data['access']}"
        )
        me_response = self.client.get(self.me_url)

        self.assertEqual(me_response.status_code, 200, me_response.data)
        self.assertEqual(
            me_response.data["active_business"]["id"],
            self.branch.id,
        )
        self.assertEqual(
            me_response.data["active_business"]["access_mode"],
            "read_only",
        )

    # --------------------------------------------------
    # ACCESS RESTRICTIONS
    # --------------------------------------------------

    def test_company_staff_cannot_switch_to_branch(self):
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
        session = self.login("staff")

        response = self.switch(
            session["access"],
            session["refresh"],
            self.branch.id,
        )

        self.assertEqual(response.status_code, 400)

    def test_company_user_cannot_switch_to_another_company(self):
        self.create_user(
            "owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        session = self.login("owner")

        response = self.switch(
            session["access"],
            session["refresh"],
            self.other_head_office.id,
        )

        self.assertEqual(response.status_code, 400)

    def test_independent_business_user_cannot_switch_business(self):
        independent = Business.objects.create(
            name="Solo Shop",
            business_type=Business.BusinessType.INDEPENDENT,
            business_code="SOLO",
        )
        user = self.create_user(
            "solo-owner",
            role=User.Role.OWNER,
            business=independent,
        )

        session = self.login("solo-owner", "SOLO")

        response = self.switch(
            session["access"],
            session["refresh"],
            self.head_office.id,
        )

        self.assertEqual(response.status_code, 400)

    # --------------------------------------------------
    # REFRESH TOKEN VALIDATION
    # --------------------------------------------------

    def test_refresh_token_from_another_user_is_rejected(self):
        self.create_user(
            "owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        self.create_user(
            "other-owner",
            role=User.Role.ADMIN,
            company=self.company,
        )

        owner_session = self.login("owner")
        other_session = self.login("other-owner")

        response = self.switch(
            owner_session["access"],
            other_session["refresh"],
            self.branch.id,
        )

        self.assertEqual(response.status_code, 400)

    def test_refresh_token_from_another_business_context_is_rejected(self):
        self.create_user(
            "owner",
            role=User.Role.OWNER,
            company=self.company,
        )

        head_office_session = self.login("owner", "ACME-HO")
        branch_session = self.login("owner", "ACME-BR")

        response = self.switch(
            head_office_session["access"],
            branch_session["refresh"],
            self.branch.id,
        )

        self.assertEqual(response.status_code, 400)

    def test_invalid_refresh_token_is_rejected(self):
        self.create_user(
            "owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        session = self.login("owner")

        response = self.switch(
            session["access"],
            "invalid-refresh-token",
            self.branch.id,
        )

        self.assertEqual(response.status_code, 400)

    # --------------------------------------------------
    # TOKEN REVOCATION AND ROTATION
    # --------------------------------------------------

    def test_successful_switch_revokes_old_refresh_token(self):
        self.create_user(
            "owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        session = self.login("owner")

        response = self.switch(
            session["access"],
            session["refresh"],
            self.branch.id,
        )

        self.assertEqual(response.status_code, 200, response.data)

        refresh_response = self.client.post(
            self.refresh_url,
            {"refresh": session["refresh"]},
            format="json",
        )

        self.assertEqual(refresh_response.status_code, 401)

    def test_new_refresh_token_works_after_switch(self):
        self.create_user(
            "owner",
            role=User.Role.OWNER,
            company=self.company,
        )
        session = self.login("owner")

        response = self.switch(
            session["access"],
            session["refresh"],
            self.branch.id,
        )

        self.assertEqual(response.status_code, 200, response.data)

        refresh_response = self.client.post(
            self.refresh_url,
            {"refresh": response.data["refresh"]},
            format="json",
        )

        self.assertEqual(
            refresh_response.status_code,
            200,
            refresh_response.data,
        )

    # --------------------------------------------------
    # PASSWORD CHANGE REQUIREMENT
    # --------------------------------------------------

    def test_user_must_change_password_before_switching_business(self):
        self.create_user(
            "newhire",
            role=User.Role.ADMIN,
            company=self.company,
            must_change_password=True,
        )
        session = self.login("newhire")

        response = self.switch(
            session["access"],
            session["refresh"],
            self.branch.id,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.data["code"],
            "password_change_required",
        )
