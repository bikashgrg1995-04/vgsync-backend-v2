from django.core.cache import cache
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.businesses.models import Business
from apps.companies.models import Company

PASSWORD = "StrongPass123!"


class OwnProfileTests(APITestCase):
    def setUp(self):
        cache.clear()
        company = Company.objects.create(name="Acme", code="ACME")
        Business.objects.create(
            name="Acme HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=company,
            business_code="ACME-HO",
        )
        self.user = User.objects.create_user(
            username="sita", password=PASSWORD, role=User.Role.ADMIN, company=company
        )
        response = self.client.post(
            "/api/v1/auth/login/",
            {"business_code": "ACME-HO", "username": "sita", "password": PASSWORD},
            format="json",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        self.url = "/api/v1/auth/me/"

    def test_changes_own_name_email_and_phone(self):
        response = self.client.patch(
            self.url,
            {"first_name": "Sita", "last_name": "Rai", "email": "s@x.com", "phone": "+977 98-12345"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["user"]["first_name"], "Sita")
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone, "+977 98-12345")

    def test_role_and_username_cannot_be_changed(self):
        self.client.patch(
            self.url, {"role": "owner", "username": "root", "first_name": "S"}, format="json"
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.role, User.Role.ADMIN)
        self.assertEqual(self.user.username, "sita")

    def test_bad_email_and_phone_are_rejected(self):
        response = self.client.patch(
            self.url, {"email": "nope", "phone": "abc"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.data)
        self.assertIn("phone", response.data)

    def test_needs_sign_in(self):
        self.client.credentials()
        self.assertEqual(self.client.patch(self.url, {}, format="json").status_code, 401)
