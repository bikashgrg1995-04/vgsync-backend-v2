from django.test import TestCase

from apps.accounts.access import (
    ACCESS_FULL,
    ACCESS_READ_ONLY,
    AccessDenied,
    serialize_active_business,
    validate_user_access,
)
from apps.accounts.models import Position, User
from apps.businesses.models import Business
from apps.companies.models import Company


class ValidateUserAccessTests(TestCase):

    def setUp(self):
        self.company = Company.objects.create(name="Acme", code="ACME")
        self.other_company = Company.objects.create(name="Other", code="OTHER")

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
        self.independent = Business.objects.create(
            name="Solo Shop",
            business_type=Business.BusinessType.INDEPENDENT,
            business_code="SOLO",
        )

        self.ho_position = Position.objects.create(
            business=self.head_office,
            name="HO Staff",
        )
        self.branch_position = Position.objects.create(
            business=self.branch,
            name="Branch Staff",
        )
        self.solo_position = Position.objects.create(
            business=self.independent,
            name="Solo Staff",
        )

    def make_user(self, username, **kwargs):
        return User.objects.create_user(
            username=username,
            password="TestPass123!",
            **kwargs,
        )

    # -- company users --------------------------------------------------

    def test_company_owner_has_full_access_to_head_office(self):
        user = self.make_user(
            "owner", role=User.Role.OWNER, company=self.company
        )

        self.assertEqual(
            validate_user_access(user, self.head_office), ACCESS_FULL
        )

    def test_company_owner_is_read_only_in_branch(self):
        user = self.make_user(
            "owner", role=User.Role.OWNER, company=self.company
        )

        self.assertEqual(
            validate_user_access(user, self.branch), ACCESS_READ_ONLY
        )

    def test_company_user_cannot_enter_another_company(self):
        user = self.make_user(
            "owner", role=User.Role.OWNER, company=self.company
        )

        with self.assertRaises(AccessDenied):
            validate_user_access(user, self.other_head_office)

    def test_company_user_cannot_enter_independent_business(self):
        user = self.make_user(
            "owner", role=User.Role.OWNER, company=self.company
        )

        with self.assertRaises(AccessDenied):
            validate_user_access(user, self.independent)

    def test_company_staff_can_enter_head_office_only(self):
        user = self.make_user(
            "staff",
            role=User.Role.STAFF,
            company=self.company,
            position=self.ho_position,
        )

        self.assertEqual(
            validate_user_access(user, self.head_office), ACCESS_FULL
        )

        with self.assertRaises(AccessDenied):
            validate_user_access(user, self.branch)

    def test_company_staff_with_position_from_another_business_is_denied(self):
        user = self.make_user(
            "staff",
            role=User.Role.STAFF,
            company=self.company,
            position=self.branch_position,
        )

        with self.assertRaises(AccessDenied):
            validate_user_access(user, self.head_office)

    # -- business users -------------------------------------------------

    def test_independent_business_owner_has_full_access(self):
        user = self.make_user(
            "solo-owner", role=User.Role.OWNER, business=self.independent
        )

        self.assertEqual(
            validate_user_access(user, self.independent), ACCESS_FULL
        )

    def test_business_user_cannot_enter_a_different_business(self):
        user = self.make_user(
            "solo-owner", role=User.Role.OWNER, business=self.independent
        )

        with self.assertRaises(AccessDenied):
            validate_user_access(user, self.head_office)

    def test_branch_user_has_full_access_to_their_own_branch(self):
        user = self.make_user(
            "branch-admin", role=User.Role.OWNER, business=self.branch
        )

        self.assertEqual(
            validate_user_access(user, self.branch), ACCESS_FULL
        )

    def test_business_staff_needs_a_position_from_their_business(self):
        user = self.make_user(
            "solo-staff",
            role=User.Role.STAFF,
            business=self.independent,
            position=self.solo_position,
        )

        self.assertEqual(
            validate_user_access(user, self.independent), ACCESS_FULL
        )

        user.position = self.ho_position

        with self.assertRaises(AccessDenied):
            validate_user_access(user, self.independent)

    def test_staff_without_position_is_denied(self):
        user = self.make_user(
            "solo-staff", role=User.Role.STAFF, business=self.independent
        )

        with self.assertRaises(AccessDenied):
            validate_user_access(user, self.independent)

    def test_staff_with_inactive_position_is_denied(self):
        self.solo_position.is_active = False
        self.solo_position.save()

        user = self.make_user(
            "solo-staff",
            role=User.Role.STAFF,
            business=self.independent,
            position=self.solo_position,
        )

        with self.assertRaises(AccessDenied):
            validate_user_access(user, self.independent)

    # -- availability and account state ---------------------------------

    def test_missing_business_is_denied(self):
        user = self.make_user(
            "owner", role=User.Role.OWNER, company=self.company
        )

        with self.assertRaises(AccessDenied):
            validate_user_access(user, None)

    def test_inactive_business_is_denied(self):
        user = self.make_user(
            "owner", role=User.Role.OWNER, company=self.company
        )
        self.head_office.is_active = False
        self.head_office.save()

        with self.assertRaises(AccessDenied):
            validate_user_access(user, self.head_office)

    def test_inactive_company_is_denied(self):
        user = self.make_user(
            "owner", role=User.Role.OWNER, company=self.company
        )
        self.company.is_active = False
        self.company.save()

        business = Business.objects.select_related("company").get(
            pk=self.head_office.pk
        )

        with self.assertRaises(AccessDenied):
            validate_user_access(user, business)

    def test_inactive_user_is_denied(self):
        user = self.make_user(
            "owner",
            role=User.Role.OWNER,
            company=self.company,
            is_active=False,
        )

        with self.assertRaises(AccessDenied):
            validate_user_access(user, self.head_office)

    def test_super_admin_is_denied(self):
        user = User.objects.create_superuser(
            username="root", password="TestPass123!"
        )

        with self.assertRaises(AccessDenied):
            validate_user_access(user, self.head_office)

    def test_access_denied_carries_a_message(self):
        with self.assertRaises(AccessDenied) as ctx:
            validate_user_access(
                self.make_user(
                    "owner", role=User.Role.OWNER, company=self.company
                ),
                None,
            )

        self.assertTrue(ctx.exception.message)

    # -- payload --------------------------------------------------------

    def test_serialize_active_business_for_company_business(self):
        business = Business.objects.select_related("company").get(
            pk=self.branch.pk
        )

        self.assertEqual(
            serialize_active_business(business, ACCESS_READ_ONLY),
            {
                "id": self.branch.id,
                "name": "Acme Branch",
                "business_code": "ACME-BR",
                "business_type": "branch",
                "company_id": self.company.id,
                "company_name": "Acme",
                "access_mode": "read_only",
            },
        )

    def test_serialize_active_business_for_independent_business(self):
        payload = serialize_active_business(self.independent, ACCESS_FULL)

        self.assertIsNone(payload["company_id"])
        self.assertIsNone(payload["company_name"])
