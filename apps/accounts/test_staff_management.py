from rest_framework.test import APITestCase

from apps.accounts.models import Position, PositionPermission, User
from apps.businesses.models import Business
from apps.common.testing import TenantTestMixin

STAFF_URL = "/api/v1/staff/"
POSITIONS_URL = "/api/v1/positions/"
CHANGE_PASSWORD_URL = "/api/v1/auth/change-password/"
REFRESH_URL = "/api/v1/auth/token/refresh/"


class StaffManagementTestCase(TenantTestMixin, APITestCase):

    def setUp(self):
        super().setUp()

        self.position = self.make_position(
            self.ho, "Sales", sales=("view", "create")
        )
        self.owner_login = self.authenticate("ACME-HO", "owner")

    def create_staff(self, **overrides):
        payload = {
            "username": "ram",
            "first_name": "Ram",
            "last_name": "Thapa",
            "role": "staff",
            "position": self.position.pk,
        }
        payload.update(overrides)

        return self.client.post(STAFF_URL, payload, format="json")


class OwnerOnboardingTests(StaffManagementTestCase):

    def test_admin_add_form_requires_password_change_by_default(self):
        root = User.objects.create_superuser("root", "root@example.com", "RootPass123!")
        self.client.force_login(root)

        response = self.client.get("/admin/accounts/user/add/")

        self.assertEqual(response.status_code, 200)
        self.assertRegex(
            response.content.decode(),
            r'name="must_change_password"[^>]*checked',
        )

    def test_new_staff_must_change_password_before_using_anything_else(self):
        response = self.create_staff()

        self.assertEqual(response.status_code, 201, response.data)
        temporary = response.data["temporary_password"]
        self.assertTrue(response.data["must_change_password"])
        self.assertNotIn("password", response.data)

        login = self.login("ACME-HO", "ram", temporary)
        self.assertEqual(login.status_code, 200, login.data)
        self.assertTrue(login.data["user"]["must_change_password"])

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {login.data['access']}"
        )

        blocked = self.client.get(STAFF_URL)
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.data["code"], "password_change_required")

        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 200)

        changed = self.client.post(
            CHANGE_PASSWORD_URL,
            {
                "current_password": temporary,
                "new_password": "BrandNewPass456!",
                "confirm_password": "BrandNewPass456!",
            },
            format="json",
        )
        self.assertEqual(changed.status_code, 200, changed.data)

        # Everything is revoked, so they sign in again with the new password.
        again = self.login("ACME-HO", "ram", "BrandNewPass456!")
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.data["user"]["must_change_password"])

    def test_owner_chosen_password_is_used_and_not_echoed(self):
        response = self.create_staff(password="OwnerChose789!")

        self.assertEqual(response.status_code, 201)
        self.assertNotIn("temporary_password", response.data)
        self.assertNotIn("password", response.data)
        self.assertEqual(
            self.login("ACME-HO", "ram", "OwnerChose789!").status_code, 200
        )

    def test_weak_password_is_rejected(self):
        response = self.create_staff(password="12345678")

        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.data)
        self.assertFalse(User.objects.filter(username="ram").exists())


class StaffCreateTests(StaffManagementTestCase):

    def test_company_staff_belong_to_the_company(self):
        response = self.create_staff()

        user = User.objects.get(pk=response.data["id"])
        self.assertEqual(user.company, self.company)
        self.assertIsNone(user.business)
        self.assertEqual(user.tenant_company, self.company)
        self.assertEqual(user.role, User.Role.STAFF)

    def test_staff_needs_a_position(self):
        response = self.create_staff(position=None)

        self.assertEqual(response.status_code, 400)
        self.assertIn("position", response.data)

    def test_position_of_another_business_is_rejected(self):
        branch_position = self.make_position(self.branch, "Branch Sales")

        response = self.create_staff(position=branch_position.pk)

        self.assertEqual(response.status_code, 400)
        self.assertIn("position", response.data)

    def test_inactive_position_is_rejected(self):
        self.position.is_active = False
        self.position.save()

        self.assertEqual(self.create_staff().status_code, 400)

    def test_username_must_be_unique_ignoring_case(self):
        self.assertEqual(self.create_staff().status_code, 201)

        response = self.create_staff(username="RAM")

        self.assertEqual(response.status_code, 400)
        self.assertIn("username", response.data)

    def test_username_is_unique_across_company_and_its_branches(self):
        self.create_staff()

        branch_owner = self.make_user(
            "bowner", User.Role.OWNER, business=self.branch
        )
        branch_position = self.make_position(self.branch, "Branch Sales")
        self.authenticate("ACME-BR", "bowner")

        response = self.client.post(
            STAFF_URL,
            {
                "username": "ram",
                "role": "staff",
                "position": branch_position.pk,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("username", response.data)

    def test_same_username_is_allowed_in_another_company(self):
        self.create_staff()

        other_position = self.make_position(self.other_ho, "Sales")
        self.authenticate("OTHER-HO", "owner")

        response = self.client.post(
            STAFF_URL,
            {"username": "ram", "role": "staff", "position": other_position.pk},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)

    def test_owner_and_super_admin_roles_cannot_be_created(self):
        for role in ("owner", "super_admin"):
            response = self.create_staff(username=f"x-{role}", role=role, position=None)

            self.assertEqual(response.status_code, 400, role)
            self.assertIn("role", response.data)

    def test_admin_and_accountant_are_positions_not_roles(self):
        for role in ("admin", "accountant"):
            response = self.create_staff(username=f"u-{role}", role=role)

            self.assertEqual(response.status_code, 400, role)
            self.assertIn("role", response.data)

    def test_branch_owner_manages_the_branch_team(self):
        self.make_user("bowner", User.Role.OWNER, business=self.branch)
        branch_position = self.make_position(self.branch, "Branch Sales")
        self.authenticate("ACME-BR", "bowner")

        response = self.client.post(
            STAFF_URL,
            {"username": "bina", "role": "staff", "position": branch_position.pk},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)

        user = User.objects.get(pk=response.data["id"])
        self.assertEqual(user.business, self.branch)
        self.assertIsNone(user.company)
        self.assertEqual(user.tenant_company, self.company)

        listing = self.client.get(STAFF_URL)
        self.assertEqual([u["username"] for u in listing.data["results"]], ["bina"])

    def test_independent_business_owner_manages_their_own_team(self):
        shop = Business.objects.create(
            name="Shop",
            business_type=Business.BusinessType.INDEPENDENT,
            business_code="SHOP",
        )
        self.make_user("shopowner", User.Role.OWNER, business=shop)
        position = self.make_position(shop, "Cashier")
        self.authenticate("SHOP", "shopowner")

        response = self.client.post(
            STAFF_URL,
            {"username": "ram", "role": "staff", "position": position.pk},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(User.objects.get(pk=response.data["id"]).business, shop)


class StaffAccessTests(StaffManagementTestCase):

    def test_only_the_owner_can_manage_the_team(self):
        self.make_user(
            "stf", User.Role.STAFF, company=self.company, position=self.position
        )

        for username in ("stf",):
            self.authenticate("ACME-HO", username)

            self.assertEqual(self.client.get(STAFF_URL).status_code, 403, username)
            self.assertEqual(
                self.client.post(STAFF_URL, {}, format="json").status_code,
                403,
                username,
            )
            self.assertEqual(self.client.get(POSITIONS_URL).status_code, 403, username)

    def test_unauthenticated_requests_are_rejected(self):
        self.client.credentials()

        self.assertEqual(self.client.get(STAFF_URL).status_code, 401)

    def test_company_owner_is_read_only_inside_a_branch(self):
        self.authenticate("ACME-BR", "owner")

        self.assertEqual(self.client.get(STAFF_URL).status_code, 200)
        self.assertEqual(self.client.get(POSITIONS_URL).status_code, 200)

        branch_position = self.make_position(self.branch, "Branch Sales")
        response = self.client.post(
            STAFF_URL,
            {"username": "x", "role": "staff", "position": branch_position.pk},
            format="json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(username="x").exists())

    def test_other_companies_staff_are_invisible(self):
        created = self.create_staff()
        staff_id = created.data["id"]

        self.authenticate("OTHER-HO", "owner")

        self.assertEqual(self.client.get(STAFF_URL).data["count"], 0)
        self.assertEqual(self.client.get(f"{STAFF_URL}{staff_id}/").status_code, 404)
        self.assertEqual(
            self.client.patch(
                f"{STAFF_URL}{staff_id}/", {"first_name": "Hacked"}, format="json"
            ).status_code,
            404,
        )
        self.assertEqual(self.client.delete(f"{STAFF_URL}{staff_id}/").status_code, 404)
        self.assertEqual(
            self.client.post(f"{STAFF_URL}{staff_id}/reset-password/").status_code, 404
        )

    def test_owners_are_not_part_of_the_managed_team(self):
        listing = self.client.get(STAFF_URL)
        self.assertEqual(listing.data["count"], 0)

        for method in (self.client.get, self.client.delete):
            self.assertEqual(method(f"{STAFF_URL}{self.owner.pk}/").status_code, 404)

        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)

    def test_list_is_paginated_searchable_and_filterable(self):
        for name in ("ram", "sita", "hari"):
            self.create_staff(username=name, first_name=name.title())

        response = self.client.get(STAFF_URL)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(
            sorted(response.data), ["count", "next", "previous", "results"]
        )

        found = self.client.get(STAFF_URL, {"search": "sit"})
        self.assertEqual([u["username"] for u in found.data["results"]], ["sita"])

        page = self.client.get(STAFF_URL, {"page_size": 2})
        self.assertEqual(len(page.data["results"]), 2)
        self.assertIsNotNone(page.data["next"])

        User.objects.filter(username="hari").update(is_active=False)
        inactive = self.client.get(STAFF_URL, {"is_active": "false"})
        self.assertEqual([u["username"] for u in inactive.data["results"]], ["hari"])


class StaffUpdateTests(StaffManagementTestCase):

    def setUp(self):
        super().setUp()

        self.staff_id = self.create_staff().data["id"]
        self.url = f"{STAFF_URL}{self.staff_id}/"

    def test_owner_edits_details(self):
        response = self.client.patch(
            self.url,
            {"first_name": "Ramesh", "phone": "9800000000"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["first_name"], "Ramesh")
        self.assertEqual(response.data["phone"], "9800000000")
        self.assertEqual(response.data["username"], "ram")

    def test_username_cannot_be_changed(self):
        self.client.patch(self.url, {"username": "other"}, format="json")

        self.assertEqual(User.objects.get(pk=self.staff_id).username, "ram")

    def test_owner_assigns_another_position(self):
        manager = self.make_position(self.ho, "Manager", sales=("view",))

        response = self.client.patch(self.url, {"position": manager.pk}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["position"], manager.pk)
        self.assertEqual(response.data["position_name"], "Manager")

    def test_position_from_another_business_is_rejected(self):
        branch_position = self.make_position(self.branch, "Branch Sales")

        response = self.client.patch(
            self.url, {"position": branch_position.pk}, format="json"
        )

        self.assertEqual(response.status_code, 400)

    def test_staff_cannot_lose_their_position(self):
        response = self.client.patch(self.url, {"position": None}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("position", response.data)

    def test_cannot_change_the_role_to_admin(self):
        response = self.client.patch(self.url, {"role": "admin"}, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("role", response.data)

    def test_cannot_promote_to_owner(self):
        response = self.client.patch(self.url, {"role": "owner"}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(User.objects.get(pk=self.staff_id).role, User.Role.STAFF)

    def test_put_and_post_to_detail_are_not_allowed(self):
        self.assertEqual(self.client.put(self.url, {}, format="json").status_code, 405)
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 405)


class StaffLifecycleTests(StaffManagementTestCase):

    def setUp(self):
        super().setUp()

        created = self.create_staff()
        self.staff_id = created.data["id"]
        self.temporary = created.data["temporary_password"]
        self.url = f"{STAFF_URL}{self.staff_id}/"

    def staff_login(self):
        """Sign the new staff in as themselves, then return to the owner."""
        data = self.login("ACME-HO", "ram", self.temporary)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self.owner_login['access']}"
        )

        return data

    def test_delete_deactivates_instead_of_removing(self):
        refresh = self.staff_login().data["refresh"]

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 204)

        user = User.objects.get(pk=self.staff_id)
        self.assertFalse(user.is_active)

        self.assertEqual(self.staff_login().status_code, 400)

        revoked = self.client.post(REFRESH_URL, {"refresh": refresh}, format="json")
        self.assertEqual(revoked.status_code, 401)

    def test_removing_one_staff_leaves_the_position_and_the_others_alone(self):
        from apps.tasks.models import Task

        other = self.create_staff(username="sita", first_name="Sita").data["id"]
        task = Task.objects.create(
            business=self.ho,
            title="Left behind",
            assigned_by=User.objects.get(pk=self.staff_id),
            assigned_to=User.objects.get(pk=other),
        )
        permissions = self.position.permissions.count()

        self.assertEqual(self.client.delete(self.url).status_code, 204)

        self.position.refresh_from_db()
        self.assertEqual(self.position.permissions.count(), permissions)
        self.assertTrue(User.objects.get(pk=other).is_active)
        self.assertEqual(User.objects.get(pk=other).position_id, self.position.pk)

        # What they created is still there, still theirs, and the person is
        # kept (as former staff) rather than erased.
        former = User.objects.get(pk=self.staff_id)
        task.refresh_from_db()
        self.assertEqual(task.assigned_by_id, former.pk)
        self.assertEqual(former.position_id, self.position.pk)

    def test_deleting_twice_is_not_an_error(self):
        self.client.delete(self.url)

        self.assertEqual(self.client.delete(self.url).status_code, 204)

    def test_reactivate_restores_sign_in(self):
        self.client.delete(self.url)

        response = self.client.post(f"{self.url}reactivate/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["is_active"])
        self.assertEqual(self.staff_login().status_code, 200)

    def test_reactivate_needs_an_active_position(self):
        self.client.delete(self.url)
        Position.objects.filter(pk=self.position.pk).update(is_active=False)

        response = self.client.post(f"{self.url}reactivate/")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "position_required")

    def test_reset_password_generates_a_temporary_one(self):
        refresh = self.staff_login().data["refresh"]

        response = self.client.post(f"{self.url}reset-password/")

        self.assertEqual(response.status_code, 200, response.data)
        new_password = response.data["temporary_password"]
        self.assertNotEqual(new_password, self.temporary)

        user = User.objects.get(pk=self.staff_id)
        self.assertTrue(user.must_change_password)

        self.assertEqual(self.login("ACME-HO", "ram", self.temporary).status_code, 400)
        self.assertEqual(self.login("ACME-HO", "ram", new_password).status_code, 200)

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self.owner_login['access']}"
        )
        revoked = self.client.post(REFRESH_URL, {"refresh": refresh}, format="json")
        self.assertEqual(revoked.status_code, 401)

    def test_reset_password_to_a_chosen_one(self):
        response = self.client.post(
            f"{self.url}reset-password/",
            {"password": "Chosen-Pass-321!"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("temporary_password", response.data)
        self.assertEqual(
            self.login("ACME-HO", "ram", "Chosen-Pass-321!").status_code, 200
        )

    def test_reset_password_rejects_a_weak_password(self):
        response = self.client.post(
            f"{self.url}reset-password/", {"password": "123"}, format="json"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.data)
        self.assertEqual(self.login("ACME-HO", "ram", self.temporary).status_code, 200)


class PositionTests(StaffManagementTestCase):

    def test_create_with_module_permissions(self):
        response = self.client.post(
            POSITIONS_URL,
            {
                "name": "Cashier",
                "description": "Counter sales",
                "permissions": [
                    {"module": "sales", "can_view": True, "can_create": True},
                    {"module": "inventory", "can_view": True},
                ],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)

        position = Position.objects.get(pk=response.data["id"])
        self.assertEqual(position.business, self.ho)

        sales = position.permissions.get(module="sales")
        self.assertTrue(sales.can_view and sales.can_create)
        self.assertFalse(sales.can_update or sales.can_delete)
        self.assertEqual(position.permissions.count(), 2)

    def test_name_is_unique_per_business_ignoring_case(self):
        response = self.client.post(
            POSITIONS_URL, {"name": "sales"}, format="json"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.data)

    def test_write_permission_requires_view(self):
        response = self.client.post(
            POSITIONS_URL,
            {"name": "X", "permissions": [{"module": "sales", "can_create": True}]},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_unknown_or_repeated_module_is_rejected(self):
        unknown = self.client.post(
            POSITIONS_URL,
            {"name": "X", "permissions": [{"module": "nope", "can_view": True}]},
            format="json",
        )
        repeated = self.client.post(
            POSITIONS_URL,
            {
                "name": "Y",
                "permissions": [
                    {"module": "sales", "can_view": True},
                    {"module": "sales", "can_view": True},
                ],
            },
            format="json",
        )

        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(repeated.status_code, 400)

    def test_update_changes_only_the_modules_sent(self):
        response = self.client.patch(
            f"{POSITIONS_URL}{self.position.pk}/",
            {"permissions": [{"module": "inventory", "can_view": True}]},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)

        modules = {p["module"]: p for p in response.data["permissions"]}
        self.assertEqual(set(modules), {"sales", "inventory"})
        self.assertTrue(modules["sales"]["can_create"])

        self.client.patch(
            f"{POSITIONS_URL}{self.position.pk}/",
            {"permissions": [{"module": "sales", "can_view": True}]},
            format="json",
        )
        sales = PositionPermission.objects.get(position=self.position, module="sales")
        self.assertFalse(sales.can_create)

    def test_staff_count_counts_active_staff_only(self):
        self.create_staff(username="a")
        self.create_staff(username="b")
        User.objects.filter(username="b").update(is_active=False)

        response = self.client.get(f"{POSITIONS_URL}{self.position.pk}/")

        self.assertEqual(response.data["staff_count"], 1)

    def test_cannot_delete_a_position_that_is_in_use(self):
        self.create_staff()

        response = self.client.delete(f"{POSITIONS_URL}{self.position.pk}/")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "position_in_use")
        self.assertTrue(Position.objects.filter(pk=self.position.pk).exists())

    def test_deactivated_staff_do_not_block_deleting_a_position(self):
        created = self.create_staff()
        staff_id = created.data["id"]
        self.client.delete(f"{STAFF_URL}{staff_id}/")

        response = self.client.delete(f"{POSITIONS_URL}{self.position.pk}/")

        self.assertEqual(response.status_code, 204)
        staff = User.objects.get(pk=staff_id)
        self.assertFalse(staff.is_active)
        self.assertIsNone(staff.position)

        # They come back only once given a position again.
        again = self.client.post(f"{STAFF_URL}{staff_id}/reactivate/")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.data["code"], "position_required")

    def test_unused_position_can_be_deleted(self):
        spare = self.make_position(self.ho, "Spare")

        response = self.client.delete(f"{POSITIONS_URL}{spare.pk}/")

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Position.objects.filter(pk=spare.pk).exists())

    def test_cannot_deactivate_a_position_with_active_staff(self):
        self.create_staff()

        response = self.client.patch(
            f"{POSITIONS_URL}{self.position.pk}/", {"is_active": False}, format="json"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("is_active", response.data)

    def test_positions_are_scoped_to_the_active_business(self):
        branch_position = self.make_position(self.branch, "Branch Sales")
        other_position = self.make_position(self.other_ho, "Other Sales")

        listing = self.client.get(POSITIONS_URL)
        names = [p["name"] for p in listing.data["results"]]

        self.assertEqual(names, ["Sales"])
        self.assertEqual(
            self.client.get(f"{POSITIONS_URL}{branch_position.pk}/").status_code, 404
        )
        self.assertEqual(
            self.client.get(f"{POSITIONS_URL}{other_position.pk}/").status_code, 404
        )

    def test_company_owner_cannot_change_positions_in_a_branch(self):
        branch_position = self.make_position(self.branch, "Branch Sales")
        self.authenticate("ACME-BR", "owner")

        self.assertEqual(
            self.client.get(f"{POSITIONS_URL}{branch_position.pk}/").status_code, 200
        )
        self.assertEqual(
            self.client.patch(
                f"{POSITIONS_URL}{branch_position.pk}/", {"name": "X"}, format="json"
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(POSITIONS_URL, {"name": "New"}, format="json").status_code,
            403,
        )

    def test_modules_lists_every_module(self):
        response = self.client.get(f"{POSITIONS_URL}modules/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {item["module"] for item in response.data},
            {value for value, _ in PositionPermission.Module.choices},
        )


class NoSignInStaffTests(StaffManagementTestCase):
    """Someone kept in the records who never uses the system."""

    def worker(self, **extra):
        payload = {
            "first_name": "Hari", "last_name": "Daily", "role": "staff",
            "can_sign_in": False, **extra,
        }

        return self.client.post(STAFF_URL, payload, format="json")

    def test_created_without_a_login_a_position_or_a_password(self):
        response = self.worker()

        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(response.data["can_sign_in"])
        self.assertNotIn("temporary_password", response.data)
        self.assertIsNone(response.data["position"])
        user = User.objects.get(pk=response.data["id"])
        self.assertFalse(user.has_usable_password())
        self.assertTrue(user.username.startswith("hari-"))
        self.assertFalse(user.must_change_password)

    def test_they_cannot_sign_in(self):
        response = self.worker(username="hari")

        self.assertEqual(self.login("ACME-HO", response.data["username"], "anything").status_code, 400)

    def test_a_password_makes_no_sense_for_them(self):
        self.assertEqual(self.worker(password="Str0ng!Passw0rd").status_code, 400)

    def test_someone_who_signs_in_still_needs_a_username_and_position(self):
        self.assertEqual(
            self.client.post(STAFF_URL, {"first_name": "A", "role": "staff", "position": self.position.pk}, format="json").status_code,
            400,
        )
        self.assertEqual(self.create_staff(position=None).status_code, 400)

    def test_their_pay_can_be_kept(self):
        staff_id = self.worker().data["id"]

        response = self.client.put(
            f"/api/v1/payroll/salaries/{staff_id}/", {"pay_type": "daily", "rate": "800.00"}, format="json"
        )

        self.assertEqual(response.status_code, 201, response.data)

    def test_sign_in_can_be_given_later_with_a_position(self):
        staff_id = self.worker().data["id"]

        without = self.client.patch(f"{STAFF_URL}{staff_id}/", {"can_sign_in": True}, format="json")
        self.assertEqual(without.status_code, 400)

        given = self.client.patch(
            f"{STAFF_URL}{staff_id}/", {"can_sign_in": True, "position": self.position.pk}, format="json"
        )

        self.assertEqual(given.status_code, 200, given.data)
        temporary = given.data["temporary_password"]
        username = given.data["username"]
        login = self.login("ACME-HO", username, temporary)
        self.assertEqual(login.status_code, 200, login.data)
        self.assertTrue(login.data["user"]["must_change_password"])

    def test_sign_in_can_be_stopped_and_records_stay(self):
        created = self.create_staff()
        staff_id = created.data["id"]
        temporary = created.data["temporary_password"]

        stopped = self.client.patch(f"{STAFF_URL}{staff_id}/", {"can_sign_in": False}, format="json")

        self.assertEqual(stopped.status_code, 200, stopped.data)
        self.assertEqual(self.login("ACME-HO", "ram", temporary).status_code, 400)
        self.assertTrue(User.objects.filter(pk=staff_id, is_active=True).exists())

    def test_no_password_reset_and_no_tasks_for_them(self):
        staff_id = self.worker().data["id"]

        reset = self.client.post(f"{STAFF_URL}{staff_id}/reset-password/", {}, format="json")
        self.assertEqual(reset.status_code, 409)
        self.assertEqual(reset.data["code"], "no_sign_in")

        assignees = self.client.get("/api/v1/tasks/assignees/").json()
        self.assertNotIn(staff_id, [p["id"] for p in assignees])

    def test_they_can_be_removed_and_brought_back_without_a_position(self):
        staff_id = self.worker().data["id"]

        self.assertEqual(self.client.delete(f"{STAFF_URL}{staff_id}/").status_code, 204)
        back = self.client.post(f"{STAFF_URL}{staff_id}/reactivate/")

        self.assertEqual(back.status_code, 200, back.data)
        self.assertTrue(back.data["is_active"])
