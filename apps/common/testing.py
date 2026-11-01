"""
Shared fixtures for API tests: a company with a Head Office, a Branch and an
owner, a second company, and helpers to sign in.
"""

from django.core.cache import cache

from apps.accounts.models import Position, PositionPermission, User
from apps.businesses.models import Business
from apps.companies.models import Company

PASSWORD = "TestPass123!"


class TenantTestMixin:
    """Use with ``APITestCase``; call ``super().setUp()`` first."""

    password = PASSWORD

    def setUp(self):
        super().setUp()

        # Throttle counters live in the cache; start every test clean.
        cache.clear()
        self.addCleanup(cache.clear)

        self.company = Company.objects.create(name="Acme", code="ACME")
        self.ho = Business.objects.create(
            name="Acme HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=self.company,
            business_code="ACME-HO",
        )
        self.branch = Business.objects.create(
            name="Acme Branch",
            business_type=Business.BusinessType.BRANCH,
            company=self.company,
            parent_business=self.ho,
            business_code="ACME-BR",
        )
        self.owner = self.make_user(
            "owner", User.Role.OWNER, company=self.company
        )

        self.other_company = Company.objects.create(name="Other", code="OTHER")
        self.other_ho = Business.objects.create(
            name="Other HO",
            business_type=Business.BusinessType.HEAD_OFFICE,
            company=self.other_company,
            business_code="OTHER-HO",
        )
        self.other_owner = self.make_user(
            "owner", User.Role.OWNER, company=self.other_company
        )

    def make_user(self, username, role, **extra):
        return User.objects.create_user(
            username=username,
            password=self.password,
            role=role,
            **extra,
        )

    def make_position(self, business, name="Sales", **modules):
        """``modules``: e.g. ``sales=("view", "create")``."""
        position = Position.objects.create(business=business, name=name)

        for module, flags in modules.items():
            PositionPermission.objects.create(
                position=position,
                module=module,
                **{f"can_{flag}": True for flag in flags},
            )

        return position

    def login(self, business_code, username, password=None):
        self.client.credentials()

        return self.client.post(
            "/api/v1/auth/login/",
            {
                "business_code": business_code,
                "username": username,
                "password": password or self.password,
            },
            format="json",
        )

    def authenticate(self, business_code, username, password=None):
        response = self.login(business_code, username, password)

        assert response.status_code == 200, response.data

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {response.data['access']}"
        )

        return response.data
