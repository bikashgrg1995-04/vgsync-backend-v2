from django.core.cache import cache
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.businesses.models import Business
from apps.companies.models import Company

PASSWORD = "TestPass123!"
NEW_PASSWORD = "BrandNewPass456!"


class TokenLifecycleTests(APITestCase):

    def setUp(self):
        cache.clear()

        self.company = Company.objects.create(name="Acme", code="ACME")
        self.head_office = Business.objects.create(
            name="Acme HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=self.company,
            business_code="ACME-HO",
        )
        self.owner = User.objects.create_user(
            username="owner",
            password=PASSWORD,
            role=User.Role.OWNER,
            company=self.company,
        )

        self.login_url = "/api/v1/auth/login/"
        self.refresh_url = "/api/v1/auth/token/refresh/"
        self.logout_url = "/api/v1/auth/logout/"
        self.me_url = "/api/v1/auth/me/"
        self.change_password_url = "/api/v1/auth/change-password/"

    def login(self, password=PASSWORD):
        return self.client.post(
            self.login_url,
            {
                "business_code": "ACME-HO",
                "username": "owner",
                "password": password,
            },
            format="json",
        )

    def refresh(self, token):
        return self.client.post(
            self.refresh_url, {"refresh": token}, format="json"
        )

    def logout(self, token):
        return self.client.post(
            self.logout_url, {"refresh": token}, format="json"
        )

    # -- rotation -------------------------------------------------------

    def test_refresh_returns_a_new_refresh_token(self):
        old = self.login().data["refresh"]

        response = self.refresh(old)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertNotEqual(response.data["refresh"], old)

    def test_used_refresh_token_cannot_be_reused(self):
        old = self.login().data["refresh"]

        self.assertEqual(self.refresh(old).status_code, 200)

        self.assertEqual(self.refresh(old).status_code, 401)

    def test_rotated_refresh_token_keeps_working(self):
        first = self.login().data["refresh"]
        second = self.refresh(first).data["refresh"]

        response = self.refresh(second)

        self.assertEqual(response.status_code, 200, response.data)

    def test_rotated_token_keeps_the_active_business_context(self):
        first = self.login().data["refresh"]
        rotated = self.refresh(first).data

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {rotated['access']}"
        )
        response = self.client.get(self.me_url)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["active_business"]["business_code"], "ACME-HO"
        )

    # -- logout ---------------------------------------------------------

    def test_logout_revokes_the_refresh_token(self):
        refresh = self.login().data["refresh"]

        response = self.logout(refresh)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.refresh(refresh).status_code, 401)

    def test_logout_twice_is_not_an_error(self):
        refresh = self.login().data["refresh"]

        self.assertEqual(self.logout(refresh).status_code, 200)
        self.assertEqual(self.logout(refresh).status_code, 200)

    def test_logout_with_garbage_token_is_not_an_error(self):
        response = self.logout("not-a-token")

        self.assertEqual(response.status_code, 200)

    def test_logout_requires_a_refresh_field(self):
        response = self.client.post(self.logout_url, {}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_logout_only_revokes_that_session(self):
        session_a = self.login().data["refresh"]
        session_b = self.login().data["refresh"]

        self.logout(session_a)

        self.assertEqual(self.refresh(session_a).status_code, 401)
        self.assertEqual(self.refresh(session_b).status_code, 200)

    # -- password change ------------------------------------------------

    def change_password(self, access, current=PASSWORD, new=NEW_PASSWORD):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

        response = self.client.post(
            self.change_password_url,
            {
                "current_password": current,
                "new_password": new,
                "confirm_password": new,
            },
            format="json",
        )

        self.client.credentials()

        return response

    def test_password_change_revokes_every_session(self):
        session_a = self.login().data
        session_b = self.login().data

        response = self.change_password(session_a["access"])

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.refresh(session_a["refresh"]).status_code, 401)
        self.assertEqual(self.refresh(session_b["refresh"]).status_code, 401)

    def test_user_can_log_in_again_with_the_new_password(self):
        session = self.login().data

        self.change_password(session["access"])

        self.assertEqual(self.login(PASSWORD).status_code, 400)
        self.assertEqual(self.login(NEW_PASSWORD).status_code, 200)

    def test_failed_password_change_does_not_revoke_sessions(self):
        session = self.login().data

        response = self.change_password(
            session["access"], current="wrong-password"
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.refresh(session["refresh"]).status_code, 200)

    def test_password_change_does_not_revoke_another_users_sessions(self):
        other = User.objects.create_user(
            username="other",
            password=PASSWORD,
            role=User.Role.OWNER,
            company=self.company,
        )
        other_session = self.client.post(
            self.login_url,
            {
                "business_code": "ACME-HO",
                "username": other.username,
                "password": PASSWORD,
            },
            format="json",
        ).data
        owner_session = self.login().data

        self.change_password(owner_session["access"])

        self.assertEqual(
            self.refresh(other_session["refresh"]).status_code, 200
        )
