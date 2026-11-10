from datetime import timedelta
from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import User
from apps.notifications.models import Notification
from apps.notifications.services import generate_everywhere, generate_for_business
from apps.sales.models import Followup, Sale
from apps.sales.test_bike_maintenance import BikeTestCase
from apps.sales.test_sales import BASE

NOTES = "/api/v1/notifications/"


def day(offset):
    return timezone.localdate() + timedelta(days=offset)


class NotificationTestCase(BikeTestCase):
    def service(self, ago=10, **extra):
        """A paid maintenance sale made ``ago`` days back: its 3 day check
        was due ``ago - 3`` days ago and its 90 day follow-up is ahead."""
        payload = {
            "sale_type": "maintenance",
            "customer": self.customer.pk,
            "sale_date": str(day(-ago)),
            "items": [{"description": "Service", "unit_price": "500.00", "quantity": "1"}],
            **extra,
        }
        response = self.client.post(BASE, payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        sale = response.json()
        self.assertEqual(self.confirm(sale["id"]).status_code, 200)

        return sale

    def notes(self, **params):
        return self.client.get(NOTES, params).json()

    def fresh(self):
        """Let the on-the-fly generation run again."""
        cache.clear()


class GeneratingTests(NotificationTestCase):
    def test_an_overdue_followup_makes_a_notification(self):
        self.service(ago=10)

        made = generate_for_business(self.ho)
        note = Notification.objects.get()

        self.assertEqual(made, 1)
        self.assertEqual(note.kind, "followup_overdue")
        self.assertIn("Ram Auto", note.title)
        self.assertIn("overdue since", note.title)
        self.assertIn("Post-service check", note.title)
        self.assertEqual(note.due_date, day(-7))
        self.assertEqual(note.business, self.ho)

    def test_one_due_today_is_due_not_overdue(self):
        self.service(ago=3)

        generate_for_business(self.ho)

        self.assertEqual(Notification.objects.get().kind, "followup_due")

    def test_future_followups_make_nothing(self):
        self.service(ago=0)

        self.assertEqual(generate_for_business(self.ho), 0)

    def test_running_again_makes_no_duplicates(self):
        self.service(ago=10)

        generate_for_business(self.ho)
        again = generate_for_business(self.ho)

        self.assertEqual(again, 0)
        self.assertEqual(Notification.objects.count(), 1)

    def test_it_becomes_overdue_the_next_day(self):
        self.service(ago=3)
        generate_for_business(self.ho, today=day(0))

        generate_for_business(self.ho, today=day(1))

        self.assertEqual(
            sorted(Notification.objects.values_list("kind", flat=True)),
            ["followup_due", "followup_overdue"],
        )

    def test_done_followups_are_not_notified(self):
        sale = self.service(ago=10)
        followup = Followup.objects.get(sale_id=sale["id"], kind="maintenance_post")
        Followup.objects.filter(pk=followup.pk).update(status="done")

        self.assertEqual(generate_for_business(self.ho), 0)

    def test_the_notes_text_includes_amount_for_credit(self):
        sale = self.made()
        self.pay(sale["id"], "500.00")
        self.assertEqual(self.confirm(sale["id"]).status_code, 200)
        Followup.objects.filter(sale_id=sale["id"], kind="credit_due").update(
            due_date=day(-2)
        )

        generate_for_business(self.ho)
        note = Notification.objects.get(followup__kind="credit_due")

        self.assertIn("amount 1000.00", note.body)
        self.assertIn("SAL-", note.body)

    def test_each_business_gets_its_own(self):
        self.service(ago=10)

        generate_everywhere()

        self.assertEqual(Notification.objects.filter(business=self.ho).count(), 1)
        self.assertEqual(Notification.objects.filter(business=self.other_ho).count(), 0)

    def test_the_daily_command(self):
        self.service(ago=10)
        out = StringIO()

        call_command("generate_notifications", stdout=out)

        self.assertIn("1 notification(s) made", out.getvalue())
        self.assertEqual(Notification.objects.count(), 1)


class ReadingTests(NotificationTestCase):
    def setUp(self):
        super().setUp()
        self.service(ago=10)
        self.fresh()

    def test_listing_makes_them_on_the_fly(self):
        listed = self.notes()

        self.assertEqual(listed["count"], 1)
        row = listed["results"][0]
        self.assertEqual(row["kind"], "followup_overdue")
        self.assertFalse(row["is_read"])
        self.assertEqual(row["customer"], self.customer.pk)
        self.assertIsNotNone(row["sale"])

    def test_unread_count_and_marking_read(self):
        self.assertEqual(self.client.get(f"{NOTES}unread-count/").json(), {"unread": 1})
        note = self.notes()["results"][0]

        marked = self.client.post(f"{NOTES}{note['id']}/read/")

        self.assertEqual(marked.status_code, 200)
        self.assertTrue(marked.json()["is_read"])
        self.assertEqual(self.client.get(f"{NOTES}unread-count/").json(), {"unread": 0})
        # Marking twice is harmless.
        self.assertEqual(self.client.post(f"{NOTES}{note['id']}/read/").status_code, 200)

    def test_unread_filter(self):
        note = self.notes()["results"][0]
        self.client.post(f"{NOTES}{note['id']}/read/")

        self.assertEqual(self.notes(unread="true")["count"], 0)
        self.assertEqual(self.notes(unread="false")["count"], 1)

    def test_read_all(self):
        self.client.get(NOTES)
        Followup.objects.filter(kind="maintenance_due").update(due_date=day(-1))
        self.fresh()
        self.client.get(NOTES)

        response = self.client.post(f"{NOTES}read-all/")

        self.assertEqual(response.json(), {"marked": 2})
        self.assertEqual(self.client.get(f"{NOTES}unread-count/").json(), {"unread": 0})

    def test_reading_is_per_person(self):
        self.make_user("boss", User.Role.OWNER, company=self.company)
        note = self.notes()["results"][0]
        self.client.post(f"{NOTES}{note['id']}/read/")

        self.authenticate("ACME-HO", "boss")

        self.assertEqual(self.client.get(f"{NOTES}unread-count/").json(), {"unread": 1})

    def test_a_notification_goes_away_once_the_followup_is_closed(self):
        sale = Sale.objects.get()
        Followup.objects.filter(sale=sale, kind="maintenance_post").update(
            status="done"
        )

        self.assertEqual(self.notes()["count"], 0)

    def test_the_other_business_sees_nothing(self):
        self.assertEqual(self.notes()["count"], 1)  # made for this business
        note = Notification.objects.get()
        self.authenticate("OTHER-HO", "owner")

        self.assertEqual(self.notes()["count"], 0)
        self.assertEqual(self.client.get(f"{NOTES}{note.pk}/").status_code, 404)
        self.assertEqual(self.client.post(f"{NOTES}{note.pk}/read/").status_code, 404)

    def test_generation_is_not_repeated_on_every_request(self):
        self.client.get(NOTES)
        Followup.objects.filter(kind="maintenance_due").update(due_date=day(-1))

        # Still inside the quiet period: nothing new yet.
        self.assertEqual(self.notes()["count"], 1)

        self.fresh()
        self.assertEqual(self.notes()["count"], 2)

    def test_the_log_cannot_be_written_to(self):
        self.assertEqual(self.client.post(NOTES, {}, format="json").status_code, 405)
        note = self.notes()["results"][0]
        self.assertEqual(self.client.delete(f"{NOTES}{note['id']}/").status_code, 405)


class WhoSeesThemTests(NotificationTestCase):
    def setUp(self):
        super().setUp()
        self.service(ago=10)
        self.fresh()

    def test_staff_need_the_reminders_permission(self):
        without = self.make_position(self.ho, "Cashier", sales=("view",))
        with_view = self.make_position(self.ho, "Caller", reminders=("view",))
        self.make_user("nope", User.Role.STAFF, company=self.company, position=without)
        self.make_user("caller", User.Role.STAFF, company=self.company, position=with_view)

        self.authenticate("ACME-HO", "nope")
        self.assertEqual(self.client.get(NOTES).status_code, 403)

        self.authenticate("ACME-HO", "caller")
        self.assertEqual(self.client.get(NOTES).status_code, 200)
        # View permission is enough to mark them read.
        note = self.notes()["results"][0]
        self.assertEqual(self.client.post(f"{NOTES}{note['id']}/read/").status_code, 200)

    def test_it_needs_a_login(self):
        self.client.credentials()

        self.assertEqual(self.client.get(NOTES).status_code, 401)

    def test_a_company_owner_sees_a_branchs_notifications_read_only(self):
        from apps.sales.models import Customer

        branch_customer = Customer.objects.create(business=self.branch, name="Branch Buyer")
        sale = Sale.objects.create(
            business=self.branch, number=1, customer=branch_customer,
            sale_type="maintenance", status="confirmed", sale_date=day(-10),
            invoice_number=1,
        )
        Followup.objects.create(
            business=self.branch, sale=sale, customer=branch_customer,
            kind="maintenance_post", due_date=day(-7),
        )

        tokens = self.authenticate("ACME-HO", "owner")
        switched = self.client.post(
            "/api/v1/auth/business-context/",
            {"refresh": tokens["refresh"], "business_id": self.branch.pk},
            format="json",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {switched.json()['access']}")
        self.fresh()

        listed = self.client.get(NOTES).json()
        self.assertEqual(listed["count"], 1)
        self.assertIn("Branch Buyer", listed["results"][0]["title"])
        note = listed["results"][0]
        self.assertEqual(self.client.post(f"{NOTES}{note['id']}/read/").status_code, 200)
