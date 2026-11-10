from rest_framework.test import APITestCase

from apps.accounts import rbac
from apps.accounts.models import User
from apps.approvals.models import ApprovalRequest, ApprovalRule, Kind
from apps.common.testing import TenantTestMixin

EXPENSES = "/api/v1/finance/expenses/"
RULES = "/api/v1/approvals/rules/"
REQUESTS = "/api/v1/approvals/"


class ApprovalTestCase(TenantTestMixin, APITestCase):
    def setUp(self):
        super().setUp()

        self.clerk = self.staff("clerk", {"finance.view", "finance.create"})
        self.manager = self.staff(
            "manager", {"finance.view", "finance.create", "finance.approve"}
        )

    def staff(self, username, codes, business=None):
        business = business or self.ho
        position = self.make_position(business, f"P-{username}")
        rbac.write_position_codes(position, codes)

        return self.make_user(
            username, User.Role.STAFF, company=self.company, position=position
        )

    def rule(self, threshold="1000", **extra):
        return ApprovalRule.objects.create(
            business=self.ho, kind=Kind.EXPENSE, threshold=threshold, **extra
        )

    def spend(self, amount, **extra):
        return self.client.post(
            EXPENSES,
            {"category": "rent", "amount": amount, **extra},
            format="json",
        )


class GateTests(ApprovalTestCase):
    def test_no_rule_means_no_approval(self):
        self.authenticate("ACME-HO", "clerk")

        self.assertEqual(self.spend("999999").status_code, 201)

    def test_under_the_threshold_goes_straight_through(self):
        self.rule("1000")
        self.authenticate("ACME-HO", "clerk")

        self.assertEqual(self.spend("999.99").status_code, 201)

    def test_at_or_over_the_threshold_is_held_not_saved(self):
        self.rule("1000")
        self.authenticate("ACME-HO", "clerk")

        response = self.spend("1000")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "approval_required")
        self.assertEqual(ApprovalRequest.objects.count(), 1)
        self.assertEqual(self.ho.expenses.count() if hasattr(self.ho, "expenses") else 0, 0)

    def test_asking_twice_reuses_the_pending_request(self):
        self.rule("1000")
        self.authenticate("ACME-HO", "clerk")

        self.spend("1000")
        self.spend("1000")

        self.assertEqual(ApprovalRequest.objects.count(), 1)

    def test_the_owner_is_never_held(self):
        self.rule("1")
        self.authenticate("ACME-HO", "owner")

        self.assertEqual(self.spend("5000").status_code, 201)

    def test_approved_request_releases_the_same_request_once(self):
        self.rule("1000")
        self.authenticate("ACME-HO", "clerk")
        approval_id = self.spend("1000").data["approval_id"]

        self.authenticate("ACME-HO", "manager")
        done = self.client.post(f"{REQUESTS}{approval_id}/approve/")
        self.assertEqual(done.status_code, 200, done.data)

        self.authenticate("ACME-HO", "clerk")
        # A different amount is a different request: refused.
        wrong = self.spend("2000", approval_id=approval_id)
        self.assertEqual(wrong.status_code, 400)

        ok = self.spend("1000", approval_id=approval_id)
        self.assertEqual(ok.status_code, 201, ok.data)

        again = self.spend("1000", approval_id=approval_id)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.data["code"], "approval_used")

    def test_pending_and_rejected_requests_do_not_release(self):
        self.rule("1000")
        self.authenticate("ACME-HO", "clerk")
        approval_id = self.spend("1000").data["approval_id"]

        pending = self.spend("1000", approval_id=approval_id)
        self.assertEqual(pending.data["code"], "approval_pending")

        self.authenticate("ACME-HO", "manager")
        self.assertEqual(
            self.client.post(f"{REQUESTS}{approval_id}/reject/", {}).status_code, 400
        )
        self.client.post(
            f"{REQUESTS}{approval_id}/reject/", {"reason": "No budget"}, format="json"
        )

        self.authenticate("ACME-HO", "clerk")
        rejected = self.spend("1000", approval_id=approval_id)
        self.assertEqual(rejected.data["code"], "approval_rejected")

    def test_someone_elses_approval_cannot_be_used(self):
        self.rule("1000")
        other = self.staff("other", {"finance.view", "finance.create"})
        self.authenticate("ACME-HO", "clerk")
        approval_id = self.spend("1000").data["approval_id"]

        self.authenticate("ACME-HO", "manager")
        self.client.post(f"{REQUESTS}{approval_id}/approve/")

        self.authenticate("ACME-HO", other.username)
        self.assertEqual(
            self.spend("1000", approval_id=approval_id).status_code, 400
        )


class DecisionRuleTests(ApprovalTestCase):
    def test_only_someone_with_the_approve_permission_can_decide(self):
        self.rule("1000")
        self.authenticate("ACME-HO", "clerk")
        approval_id = self.spend("1000").data["approval_id"]

        # The creator holds no approve permission.
        self.assertEqual(
            self.client.post(f"{REQUESTS}{approval_id}/approve/").status_code, 403
        )

    def test_you_cannot_approve_your_own_request_by_default(self):
        self.rule("1000")
        self.authenticate("ACME-HO", "manager")
        approval_id = self.spend("1000").data["approval_id"]

        response = self.client.post(f"{REQUESTS}{approval_id}/approve/")

        self.assertEqual(response.status_code, 403)

    def test_self_approval_can_be_allowed_by_the_owner_rule(self):
        self.rule("1000", allow_self_approval=True)
        self.authenticate("ACME-HO", "manager")
        approval_id = self.spend("1000").data["approval_id"]

        self.assertEqual(
            self.client.post(f"{REQUESTS}{approval_id}/approve/").status_code, 200
        )

    def test_a_decided_request_cannot_be_decided_again(self):
        self.rule("1000")
        self.authenticate("ACME-HO", "clerk")
        approval_id = self.spend("1000").data["approval_id"]

        self.authenticate("ACME-HO", "manager")
        self.client.post(f"{REQUESTS}{approval_id}/approve/")

        self.assertEqual(
            self.client.post(f"{REQUESTS}{approval_id}/approve/").status_code, 409
        )

    def test_another_business_cannot_see_or_decide(self):
        self.rule("1000")
        self.authenticate("ACME-HO", "clerk")
        approval_id = self.spend("1000").data["approval_id"]

        self.authenticate("OTHER-HO", "owner")
        self.assertEqual(self.client.get(REQUESTS).data["count"], 0)
        self.assertEqual(
            self.client.post(f"{REQUESTS}{approval_id}/approve/").status_code, 404
        )


class RuleApiTests(ApprovalTestCase):
    def test_only_the_owner_sets_thresholds(self):
        self.authenticate("ACME-HO", "manager")
        body = {"kind": "expense", "threshold": "500"}

        self.assertEqual(self.client.post(RULES, body, format="json").status_code, 403)

        self.authenticate("ACME-HO", "owner")
        self.assertEqual(self.client.post(RULES, body, format="json").status_code, 201)

    def test_posting_the_same_kind_updates_it(self):
        self.authenticate("ACME-HO", "owner")

        self.client.post(RULES, {"kind": "expense", "threshold": "500"}, format="json")
        self.client.post(RULES, {"kind": "expense", "threshold": "900"}, format="json")

        self.assertEqual(ApprovalRule.objects.count(), 1)
        self.assertEqual(str(ApprovalRule.objects.get().threshold), "900.00")

    def test_a_negative_threshold_is_refused(self):
        self.authenticate("ACME-HO", "owner")

        response = self.client.post(
            RULES, {"kind": "expense", "threshold": "-1"}, format="json"
        )

        self.assertEqual(response.status_code, 400)
