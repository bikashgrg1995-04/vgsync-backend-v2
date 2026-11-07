from django.test import override_settings
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.common.testing import TenantTestMixin

from .models import Faq, SupportTicket

FAQS = "/api/v1/support/faqs/"
TICKETS = "/api/v1/support/tickets/"
CONTACT = "/api/v1/support/contact/"

PAYLOAD = {
    "subject": "Stock not updating",
    "category": "inventory",
    "description": "The stock count stays the same after I confirm a purchase.",
}


class SupportTestCase(TenantTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate("ACME-HO", "owner")

    def raise_ticket(self, **extra):
        return self.client.post(TICKETS, {**PAYLOAD, **extra}, format="json")


class FaqTests(SupportTestCase):
    def test_the_migration_seeds_the_faqs(self):
        questions = [faq["question"] for faq in self.client.get(FAQS).json()["results"]]

        self.assertEqual(questions[0], "How to add a new product?")
        self.assertIn("Can I get a refund?", questions)

    def test_only_published_faqs_are_listed_in_order(self):
        Faq.objects.all().delete()
        Faq.objects.create(question="Second", answer="b", sort_order=2)
        Faq.objects.create(question="First", answer="a", sort_order=1)
        Faq.objects.create(question="Hidden", answer="c", is_published=False)

        results = self.client.get(FAQS).json()["results"]

        self.assertEqual([faq["question"] for faq in results], ["First", "Second"])
        self.assertEqual(set(results[0]), {"id", "question", "answer"})

    def test_search_looks_in_question_and_answer(self):
        results = self.client.get(FAQS, {"search": "Excel"}).json()["results"]

        self.assertEqual([faq["question"] for faq in results], ["How to generate sales report?"])

    def test_faqs_cannot_be_written_through_the_api(self):
        self.assertEqual(self.client.post(FAQS, {}, format="json").status_code, 405)

    def test_it_needs_a_login(self):
        self.client.credentials()

        self.assertEqual(self.client.get(FAQS).status_code, 401)


class RaisingATicketTests(SupportTestCase):
    def test_a_ticket_is_made_for_the_active_business_and_user(self):
        response = self.raise_ticket()
        data = response.json()
        ticket = SupportTicket.objects.get()

        self.assertEqual(response.status_code, 201, data)
        self.assertEqual(ticket.business, self.ho)
        self.assertEqual(ticket.created_by, self.owner)
        self.assertEqual(data["reference"], f"TKT-{ticket.pk:05d}")
        self.assertEqual(data["status"], "open")
        self.assertEqual(data["status_label"], "Open")
        self.assertEqual(data["category_label"], "Inventory")
        self.assertEqual(data["created_by_name"], "owner")

    def test_people_cannot_choose_the_status_or_business(self):
        response = self.raise_ticket(
            status="resolved", business=self.other_ho.pk, created_by=999
        )
        ticket = SupportTicket.objects.get()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(ticket.status, "open")
        self.assertEqual(ticket.business, self.ho)
        self.assertEqual(ticket.created_by, self.owner)

    def test_the_category_is_optional(self):
        payload = {k: v for k, v in PAYLOAD.items() if k != "category"}

        response = self.client.post(TICKETS, payload, format="json")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["category"], "other")

    def test_bad_input_is_refused(self):
        self.assertEqual(self.raise_ticket(subject="  ").status_code, 400)
        self.assertEqual(self.raise_ticket(subject="Hi").status_code, 400)
        self.assertEqual(self.raise_ticket(description="too short").status_code, 400)
        self.assertEqual(self.raise_ticket(category="nonsense").status_code, 400)
        self.assertEqual(SupportTicket.objects.count(), 0)

    def test_text_is_trimmed(self):
        response = self.raise_ticket(subject="  Report export issue  ")

        self.assertEqual(response.json()["subject"], "Report export issue")

    def test_a_company_owner_can_raise_one_while_read_only_in_a_branch(self):
        self.authenticate("ACME-BR", "owner")

        response = self.raise_ticket()

        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(SupportTicket.objects.get().business, self.branch)

    def test_a_ticket_cannot_be_changed_or_deleted_through_the_api(self):
        pk = self.raise_ticket().json()["id"]

        self.assertEqual(
            self.client.patch(f"{TICKETS}{pk}/", {"status": "resolved"}, format="json").status_code,
            405,
        )
        self.assertEqual(self.client.delete(f"{TICKETS}{pk}/").status_code, 405)

    def test_raising_tickets_is_rate_limited(self):
        for _ in range(20):
            self.assertEqual(self.raise_ticket().status_code, 201)

        response = self.raise_ticket()

        self.assertEqual(response.status_code, 429)
        self.assertEqual(SupportTicket.objects.count(), 20)


class WhoSeesTicketsTests(SupportTestCase):
    def setUp(self):
        super().setUp()
        self.raise_ticket(subject="Owner ticket")

        position = self.make_position(self.branch, "Clerk")
        self.staff = self.make_user(
            "clerk", User.Role.STAFF, business=self.branch, position=position
        )
        self.authenticate("ACME-BR", "clerk")
        self.raise_ticket(subject="Clerk ticket")

        self.client.credentials()

    def subjects(self, business_code, username):
        self.authenticate(business_code, username)

        return [t["subject"] for t in self.client.get(TICKETS).json()["results"]]

    def test_the_owner_sees_every_ticket_of_the_business(self):
        self.assertEqual(self.subjects("ACME-HO", "owner"), ["Owner ticket"])
        self.assertEqual(self.subjects("ACME-BR", "owner"), ["Clerk ticket"])

    def test_staff_see_only_their_own(self):
        self.assertEqual(self.subjects("ACME-BR", "clerk"), ["Clerk ticket"])

    def test_a_branch_owner_sees_the_clerks_ticket(self):
        self.make_user("boss", User.Role.OWNER, business=self.branch)

        self.assertEqual(self.subjects("ACME-BR", "boss"), ["Clerk ticket"])

    def test_staff_cannot_open_a_ticket_they_did_not_raise(self):
        self.authenticate("ACME-HO", "owner")
        owner_ticket = SupportTicket.objects.get(subject="Owner ticket")
        branch_ticket = SupportTicket.objects.get(subject="Clerk ticket")

        self.authenticate("ACME-BR", "clerk")

        self.assertEqual(self.client.get(f"{TICKETS}{branch_ticket.pk}/").status_code, 200)
        self.assertEqual(self.client.get(f"{TICKETS}{owner_ticket.pk}/").status_code, 404)

    def test_another_company_never_sees_them(self):
        self.assertEqual(self.subjects("OTHER-HO", "owner"), [])

        ticket = SupportTicket.objects.first()

        self.assertEqual(self.client.get(f"{TICKETS}{ticket.pk}/").status_code, 404)

    def test_it_needs_a_login(self):
        self.client.credentials()

        self.assertEqual(self.client.get(TICKETS).status_code, 401)
        self.assertEqual(self.client.post(TICKETS, PAYLOAD, format="json").status_code, 401)


class FilteringTests(SupportTestCase):
    def setUp(self):
        super().setUp()
        self.raise_ticket(subject="Stock not updating", category="inventory")
        self.raise_ticket(subject="Report export issue", category="reports")
        SupportTicket.objects.filter(subject="Report export issue").update(status="resolved")

    def subjects(self, **params):
        return [t["subject"] for t in self.client.get(TICKETS, params).json()["results"]]

    def test_newest_first(self):
        self.assertEqual(self.subjects(), ["Report export issue", "Stock not updating"])

    def test_filter_by_status_and_category(self):
        self.assertEqual(self.subjects(status="resolved"), ["Report export issue"])
        self.assertEqual(self.subjects(category="inventory"), ["Stock not updating"])

    def test_search(self):
        self.assertEqual(self.subjects(search="export"), ["Report export issue"])


class ContactTests(SupportTestCase):
    def test_it_returns_the_contact_details(self):
        data = self.client.get(CONTACT).json()

        self.assertEqual(data["email"], "support@vgsync.com")
        self.assertEqual(data["phone"], "+977-1-5901234")
        self.assertEqual(data["hours"], "Mon - Sat, 9AM - 6PM")
        self.assertEqual(data["user_guide_url"], "")

    @override_settings(SUPPORT_USER_GUIDE_URL="https://example.com/guide.pdf")
    def test_the_links_come_from_settings(self):
        self.assertEqual(
            self.client.get(CONTACT).json()["user_guide_url"],
            "https://example.com/guide.pdf",
        )

    def test_it_needs_a_login(self):
        self.client.credentials()

        self.assertEqual(self.client.get(CONTACT).status_code, 401)
