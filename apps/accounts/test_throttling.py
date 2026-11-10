from django.core.cache import cache
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.businesses.models import Business
from apps.companies.models import Company

PASSWORD = "TestPass123!"
NEW_PASSWORD = "BrandNewPass456!"


class AuthThrottlingTests(APITestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

        self.company = Company.objects.create(name="Acme", code="ACME")
        self.head_office = Business.objects.create(
            name="Acme HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=self.company,
            business_code="ACME-HO",
        )
        for username in ("alice", "bob"):
            User.objects.create_user(
                username=username,
                password=PASSWORD,
                role=User.Role.OWNER,
                company=self.company,
            )

        self.login_url = "/api/v1/auth/login/"
        self.refresh_url = "/api/v1/auth/token/refresh/"
        self.logout_url = "/api/v1/auth/logout/"
        self.change_password_url = "/api/v1/auth/change-password/"

    def login(self, username="alice", password=PASSWORD, ip="10.0.0.1"):
        return self.client.post(
            self.login_url,
            {
                "business_code": "ACME-HO",
                "username": username,
                "password": password,
            },
            format="json",
            REMOTE_ADDR=ip,
        )

    # -- per-account limit ---------------------------------------------

    def test_repeated_wrong_passwords_are_throttled_per_account(self):
        for _ in range(5):
            self.assertEqual(self.login(password="wrong").status_code, 400)

        response = self.login(password="wrong")

        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)

    def test_correct_password_is_also_blocked_once_throttled(self):
        for _ in range(5):
            self.login(password="wrong")

        self.assertEqual(self.login().status_code, 429)

    def test_account_limit_applies_across_different_ip_addresses(self):
        for i in range(5):
            self.login(password="wrong", ip=f"10.0.1.{i}")

        response = self.login(password="wrong", ip="10.0.9.9")

        self.assertEqual(response.status_code, 429)

    def test_throttled_account_does_not_lock_out_other_accounts(self):
        for _ in range(5):
            self.login(username="alice", password="wrong")

        self.assertEqual(self.login(username="alice").status_code, 429)
        self.assertEqual(self.login(username="bob").status_code, 200)

    def test_username_case_is_not_a_way_around_the_business_code_limit(self):
        for _ in range(5):
            self.login(password="wrong")

        response = self.client.post(
            self.login_url,
            {
                "business_code": "acme-ho",
                "username": "alice",
                "password": "wrong",
            },
            format="json",
            REMOTE_ADDR="10.0.0.1",
        )

        self.assertEqual(response.status_code, 429)

    def test_successful_logins_count_toward_the_limit_too(self):
        for _ in range(5):
            self.assertEqual(self.login().status_code, 200)

        self.assertEqual(self.login().status_code, 429)

    # -- per-IP limit ----------------------------------------------------

    def test_one_ip_is_limited_even_when_guessing_different_accounts(self):
        for i in range(30):
            self.login(username=f"user{i}", password="wrong")

        response = self.login(username="user-new", password="wrong")

        self.assertEqual(response.status_code, 429)

    def test_a_different_ip_is_not_affected_by_the_ip_limit(self):
        for i in range(30):
            self.login(username=f"user{i}", password="wrong", ip="10.0.0.1")

        self.assertEqual(
            self.login(username="alice", ip="10.0.0.2").status_code, 200
        )

    def test_spoofed_forwarded_for_header_does_not_dodge_the_limit(self):
        # NUM_PROXIES is 0 by default, so X-Forwarded-For must be ignored.
        for i in range(30):
            self.client.post(
                self.login_url,
                {
                    "business_code": "ACME-HO",
                    "username": f"user{i}",
                    "password": "wrong",
                },
                format="json",
                REMOTE_ADDR="10.0.0.1",
                HTTP_X_FORWARDED_FOR=f"203.0.113.{i}",
            )

        response = self.client.post(
            self.login_url,
            {
                "business_code": "ACME-HO",
                "username": "another",
                "password": "wrong",
            },
            format="json",
            REMOTE_ADDR="10.0.0.1",
            HTTP_X_FORWARDED_FOR="203.0.113.250",
        )

        self.assertEqual(response.status_code, 429)

    # -- malformed requests ---------------------------------------------

    def test_requests_without_credentials_do_not_hit_the_account_limit(self):
        for _ in range(8):
            response = self.client.post(
                self.login_url, {}, format="json", REMOTE_ADDR="10.0.0.1"
            )
            self.assertEqual(response.status_code, 400)

    def test_non_object_json_body_does_not_crash_the_throttle(self):
        response = self.client.post(
            self.login_url,
            ["not", "an", "object"],
            format="json",
            REMOTE_ADDR="10.0.0.1",
        )

        self.assertEqual(response.status_code, 400)

    # -- other endpoints --------------------------------------------------

    def test_refresh_is_throttled_per_ip(self):
        for _ in range(60):
            self.assertEqual(
                self.client.post(
                    self.refresh_url,
                    {"refresh": "bad"},
                    format="json",
                    REMOTE_ADDR="10.0.0.1",
                ).status_code,
                401,
            )

        response = self.client.post(
            self.refresh_url,
            {"refresh": "bad"},
            format="json",
            REMOTE_ADDR="10.0.0.1",
        )

        self.assertEqual(response.status_code, 429)

    def test_logout_is_throttled_per_ip(self):
        for _ in range(60):
            self.client.post(
                self.logout_url,
                {"refresh": "bad"},
                format="json",
                REMOTE_ADDR="10.0.0.1",
            )

        response = self.client.post(
            self.logout_url,
            {"refresh": "bad"},
            format="json",
            REMOTE_ADDR="10.0.0.1",
        )

        self.assertEqual(response.status_code, 429)

    def test_change_password_attempts_are_throttled_per_user(self):
        access = self.login().data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

        payload = {
            "current_password": "wrong",
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        }

        for _ in range(10):
            self.assertEqual(
                self.client.post(
                    self.change_password_url, payload, format="json"
                ).status_code,
                400,
            )

        response = self.client.post(
            self.change_password_url, payload, format="json"
        )

        self.assertEqual(response.status_code, 429)
