from types import SimpleNamespace

from django.core.cache import cache
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory, APITestCase

from apps.accounts.models import User
from apps.businesses.models import Business
from apps.common.authentication import VGSyncJWTAuthentication
from apps.common.exceptions import PasswordChangeRequired
from apps.companies.models import Company

TEMP_PASSWORD = "TempPass123!"
NEW_PASSWORD = "BrandNewPass456!"


class MustChangePasswordTests(APITestCase):

    def setUp(self):
        cache.clear()

        self.company = Company.objects.create(name="Acme", code="ACME")
        self.head_office = Business.objects.create(
            name="Acme HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=self.company,
            business_code="ACME-HO",
        )
        self.user = User.objects.create_user(
            username="newhire",
            password=TEMP_PASSWORD,
            role=User.Role.OWNER,
            company=self.company,
            must_change_password=True,
        )

        self.login_url = "/api/v1/auth/login/"
        self.me_url = "/api/v1/auth/me/"
        self.change_password_url = "/api/v1/auth/change-password/"

    def login(self, password=TEMP_PASSWORD):
        return self.client.post(
            self.login_url,
            {
                "business_code": "ACME-HO",
                "username": "newhire",
                "password": password,
            },
            format="json",
        )

    def change_password(self, access, current, new):
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

    def authenticate_as_view(self, access, view_name):
        """Run the auth class as if ``view_name`` were being called."""
        django_request = APIRequestFactory().get(
            "/some/path/",
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )

        if view_name is not None:
            django_request.resolver_match = SimpleNamespace(
                view_name=view_name
            )

        return VGSyncJWTAuthentication().authenticate(
            Request(django_request)
        )

    # -- login and allowed endpoints -----------------------------------

    def test_login_still_works_and_reports_the_flag(self):
        response = self.login()

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["user"]["must_change_password"])

    def test_me_is_allowed_while_the_flag_is_set(self):
        access = self.login().data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

        response = self.client.get(self.me_url)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["user"]["must_change_password"])

    def test_change_password_is_allowed_while_the_flag_is_set(self):
        access = self.login().data["access"]

        response = self.change_password(access, TEMP_PASSWORD, NEW_PASSWORD)

        self.assertEqual(response.status_code, 200, response.data)

    # -- blocked endpoints ---------------------------------------------

    def test_any_other_view_is_blocked_with_a_machine_readable_code(self):
        access = self.login().data["access"]

        with self.assertRaises(PasswordChangeRequired) as ctx:
            self.authenticate_as_view(access, "inventory:product-list")

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(
            ctx.exception.detail["code"], "password_change_required"
        )

    def test_unknown_view_fails_closed(self):
        access = self.login().data["access"]

        with self.assertRaises(PasswordChangeRequired):
            self.authenticate_as_view(access, None)

    def test_other_views_are_allowed_once_the_flag_is_cleared(self):
        self.user.must_change_password = False
        self.user.save()
        access = self.login().data["access"]

        user, _ = self.authenticate_as_view(access, "inventory:product-list")

        self.assertEqual(user.pk, self.user.pk)

    def test_flag_set_after_login_applies_to_the_existing_token(self):
        self.user.must_change_password = False
        self.user.save()
        access = self.login().data["access"]

        # An admin resets the password while the user is signed in.
        self.user.must_change_password = True
        self.user.save()

        with self.assertRaises(PasswordChangeRequired):
            self.authenticate_as_view(access, "inventory:product-list")

    # -- changing the password ----------------------------------------

    def test_changing_the_password_clears_the_flag(self):
        access = self.login().data["access"]

        self.change_password(access, TEMP_PASSWORD, NEW_PASSWORD)

        self.user.refresh_from_db()
        self.assertFalse(self.user.must_change_password)

        login = self.login(NEW_PASSWORD)
        self.assertEqual(login.status_code, 200, login.data)
        self.assertFalse(login.data["user"]["must_change_password"])

        user, _ = self.authenticate_as_view(
            login.data["access"], "inventory:product-list"
        )
        self.assertEqual(user.pk, self.user.pk)

    def test_new_password_must_differ_from_the_current_one(self):
        access = self.login().data["access"]

        response = self.change_password(access, TEMP_PASSWORD, TEMP_PASSWORD)

        self.assertEqual(response.status_code, 400)
        self.assertIn("new_password", response.data)

        self.user.refresh_from_db()
        self.assertTrue(self.user.must_change_password)
