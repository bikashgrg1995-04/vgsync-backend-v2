import re

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.businesses.models import Business
from apps.companies.models import Company

PASSWORD = "TestPass123!"
NEW_PASSWORD = "BrandNewPass456!"


class PasswordResetTests(APITestCase):

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
        self.user = User.objects.create_user(
            username="alice",
            email="alice@example.com",
            password=PASSWORD,
            role=User.Role.OWNER,
            company=self.company,
            must_change_password=True,
        )
        self.no_email = User.objects.create_user(
            username="bob",
            password=PASSWORD,
            role=User.Role.OWNER,
            company=self.company,
        )

        self.forgot_url = "/api/v1/auth/forgot-password/"
        self.reset_url = "/api/v1/auth/reset-password/"
        self.login_url = "/api/v1/auth/login/"
        self.refresh_url = "/api/v1/auth/token/refresh/"

    def forgot(self, username="alice", code="ACME-HO", ip="10.0.0.1"):
        return self.client.post(
            self.forgot_url,
            {"business_code": code, "username": username},
            format="json",
            REMOTE_ADDR=ip,
        )

    def login(self, password):
        return self.client.post(
            self.login_url,
            {
                "business_code": "ACME-HO",
                "username": "alice",
                "password": password,
            },
            format="json",
        )

    def link_params(self):
        body = mail.outbox[-1].body
        match = re.search(r"reset-password\?uid=([^&\s]+)&token=(\S+)", body)
        self.assertIsNotNone(match, body)
        return match.group(1), match.group(2)

    def reset(self, uid, token, new=NEW_PASSWORD, confirm=None):
        return self.client.post(
            self.reset_url,
            {
                "uid": uid,
                "token": token,
                "new_password": new,
                "confirm_password": confirm or new,
            },
            format="json",
        )

    # -- forgot password -------------------------------------------------

    def test_sends_email_with_frontend_link(self):
        response = self.forgot()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["alice@example.com"])
        self.assertIn("/reset-password?uid=", mail.outbox[0].body)

    def test_reply_is_identical_for_unknown_account_or_missing_email(self):
        known = self.forgot()
        unknown_user = self.forgot(username="ghost")
        unknown_business = self.forgot(code="NOPE")
        no_email = self.forgot(username="bob")

        for response in (unknown_user, unknown_business, no_email):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, known.data)

        # Only the real account with an email got a message.
        self.assertEqual(len(mail.outbox), 1)

    def test_inactive_user_gets_no_email(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)

        self.assertEqual(self.forgot().status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    def test_requires_both_fields(self):
        response = self.client.post(
            self.forgot_url, {"username": "alice"}, format="json"
        )

        self.assertEqual(response.status_code, 400)

    def test_account_throttle(self):
        for i in range(3):
            self.assertEqual(self.forgot(ip=f"10.0.1.{i}").status_code, 200)

        self.assertEqual(self.forgot(ip="10.0.1.99").status_code, 429)

    # -- reset password --------------------------------------------------

    def test_reset_changes_password_and_clears_forced_change(self):
        self.forgot()
        uid, token = self.link_params()

        response = self.reset(uid, token)

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertFalse(self.user.must_change_password)
        self.assertEqual(self.login(NEW_PASSWORD).status_code, 200)
        self.assertEqual(self.login(PASSWORD).status_code, 400)

    def test_link_is_single_use(self):
        self.forgot()
        uid, token = self.link_params()

        self.assertEqual(self.reset(uid, token).status_code, 200)

        again = self.reset(uid, token, new="AnotherPass789!")
        self.assertEqual(again.status_code, 400)

    def test_reset_signs_out_existing_sessions(self):
        refresh = self.login(PASSWORD).data["refresh"]

        self.forgot()
        uid, token = self.link_params()
        self.reset(uid, token)

        response = self.client.post(
            self.refresh_url, {"refresh": refresh}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_bad_token_or_uid_rejected(self):
        self.forgot()
        uid, token = self.link_params()

        self.assertEqual(self.reset(uid, "bad-token").status_code, 400)
        self.assertEqual(self.reset("not-a-uid", token).status_code, 400)
        self.assertEqual(self.reset("", token).status_code, 400)

    def test_token_for_other_user_rejected(self):
        self.forgot()
        _, token = self.link_params()

        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        other_uid = urlsafe_base64_encode(force_bytes(self.no_email.pk))

        self.assertEqual(self.reset(other_uid, token).status_code, 400)

    @override_settings(PASSWORD_RESET_TIMEOUT=-1)
    def test_expired_token_rejected(self):
        self.forgot()
        uid, token = self.link_params()

        self.assertEqual(self.reset(uid, token).status_code, 400)

    def test_mismatched_confirmation_rejected(self):
        self.forgot()
        uid, token = self.link_params()

        response = self.reset(uid, token, confirm="Different123!")

        self.assertEqual(response.status_code, 400)
        self.assertIn("confirm_password", response.data)

    def test_weak_password_rejected_and_link_still_valid(self):
        self.forgot()
        uid, token = self.link_params()

        self.assertEqual(self.reset(uid, token, new="12345678").status_code, 400)
        self.assertEqual(self.reset(uid, token).status_code, 200)

    def test_super_admin_cannot_reset_through_api(self):
        admin = User.objects.create_superuser(
            username="root", email="root@example.com", password=PASSWORD
        )
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        uid = urlsafe_base64_encode(force_bytes(admin.pk))
        token = default_token_generator.make_token(admin)

        self.assertEqual(self.reset(uid, token).status_code, 400)
