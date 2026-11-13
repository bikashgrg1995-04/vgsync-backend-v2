from datetime import timedelta

from django.utils import timezone

from apps.accounts.models import User
from apps.notifications.models import Notification
from apps.sales.test_sale_returns import ReturnTestCase
from apps.tasks.models import Task

TASKS = "/api/v1/tasks/"
NOTES = "/api/v1/notifications/"


class TaskTests(ReturnTestCase):
    def setUp(self):
        super().setUp()
        position = self.make_position(self.ho, "Helper", sales=("view",))
        self.ram = self.make_user("ram", User.Role.STAFF, company=self.company, position=position)
        self.sita = self.make_user("sita", User.Role.STAFF, company=self.company, position=position)

    def assign(self, to=None, **extra):
        body = {"title": "Count the oil stock", "assigned_to": (to or self.ram).pk, **extra}

        return self.client.post(TASKS, body, format="json")

    def as_user(self, name):
        self.authenticate("ACME-HO", name)

    def notes(self):
        response = self.client.get(NOTES)

        return response.json()["results"]

    # -- assigning ------------------------------------------------------

    def test_owner_assigns_a_task_and_the_staff_member_is_notified(self):
        response = self.assign(due_date=str(timezone.localdate() + timedelta(days=2)), priority="high")

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["status"], "todo")
        self.assertEqual(response.json()["events"][0]["kind"], "created")

        self.as_user("ram")
        notes = self.notes()
        self.assertEqual([n["kind"] for n in notes], ["task_assigned"])
        self.assertIn("Count the oil stock", notes[0]["title"])
        self.assertTrue(notes[0]["link"])

        # The other staff member is told nothing.
        self.as_user("sita")
        self.assertEqual(self.notes(), [])

    def test_a_task_can_only_go_to_someone_in_this_business(self):
        stranger = self.make_user("stranger", User.Role.STAFF, business=self.branch)

        response = self.assign(to=stranger)

        self.assertEqual(response.status_code, 400)

    def test_plain_staff_cannot_assign(self):
        self.as_user("ram")

        self.assertEqual(self.assign(to=self.sita).status_code, 403)

    # -- doing the work -------------------------------------------------

    def test_the_assignee_moves_it_along_and_the_owner_is_told_when_done(self):
        task = self.assign().json()["id"]
        self.as_user("ram")

        started = self.client.post(f"{TASKS}{task}/status/", {"status": "in_progress"}, format="json")
        done = self.client.post(f"{TASKS}{task}/status/", {"status": "done", "note": "Counted: 12"}, format="json")

        self.assertEqual(started.status_code, 200, started.content)
        self.assertEqual(done.json()["status"], "done")
        self.assertIsNotNone(done.json()["completed_at"])
        self.assertEqual([e["kind"] for e in done.json()["events"]], ["created", "status", "status"])

        self.authenticate("ACME-HO", "owner")
        kinds = [n["kind"] for n in self.notes()]
        self.assertIn("task_done", kinds)

    def test_someone_else_cannot_change_the_status(self):
        task = self.assign().json()["id"]
        self.as_user("sita")

        # She cannot even see it.
        self.assertEqual(self.client.post(f"{TASKS}{task}/status/", {"status": "done"}, format="json").status_code, 404)

    def test_comments_notify_the_other_party(self):
        task = self.assign().json()["id"]
        self.as_user("ram")

        self.client.post(f"{TASKS}{task}/comment/", {"note": "Need the key"}, format="json")

        self.authenticate("ACME-HO", "owner")
        self.assertIn("task_comment", [n["kind"] for n in self.notes()])

    def test_staff_see_only_their_own_tasks_and_managers_see_all(self):
        self.assign(to=self.ram)
        self.assign(to=self.sita, title="Wash the floor")

        self.as_user("ram")
        mine = self.client.get(TASKS).json()["results"]
        self.assertEqual([t["title"] for t in mine], ["Count the oil stock"])

        self.authenticate("ACME-HO", "owner")
        self.assertEqual(len(self.client.get(TASKS).json()["results"]), 2)

    # -- changing and tracking -----------------------------------------

    def test_reassigning_tells_both_people_and_is_in_the_history(self):
        task = self.assign().json()["id"]

        response = self.client.patch(f"{TASKS}{task}/", {"assigned_to": self.sita.pk}, format="json")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("reassigned", [e["kind"] for e in response.json()["events"]])
        self.as_user("sita")
        self.assertIn("task_assigned", [n["kind"] for n in self.notes()])
        self.as_user("ram")
        self.assertIn("task_updated", [n["kind"] for n in self.notes()])

    def test_cancelling_keeps_the_task_and_tells_the_assignee(self):
        task = self.assign().json()["id"]

        response = self.client.post(f"{TASKS}{task}/cancel/", {"note": "Not needed"}, format="json")

        self.assertEqual(response.json()["status"], "cancelled")
        self.assertTrue(Task.objects.filter(pk=task).exists())
        self.assertEqual(self.client.delete(f"{TASKS}{task}/").status_code, 405)

    def test_summary_counts_open_overdue_and_per_person(self):
        yesterday = str(timezone.localdate() - timedelta(days=1))
        self.assign(due_date=yesterday)
        done = self.assign(to=self.sita, title="Other").json()["id"]
        self.as_user("sita")
        self.client.post(f"{TASKS}{done}/status/", {"status": "done"}, format="json")

        self.authenticate("ACME-HO", "owner")
        summary = self.client.get(TASKS + "summary/").json()

        self.assertEqual(summary["team"]["open"], 1)
        self.assertEqual(summary["team"]["overdue"], 1)
        self.assertEqual(summary["team"]["done_this_week"], 1)
        self.assertEqual({p["name"] for p in summary["people"]}, {"ram", "sita"})

    def test_an_overdue_task_gets_one_heads_up_for_each_person(self):
        from apps.tasks.services import generate_overdue

        self.assign(due_date=str(timezone.localdate() - timedelta(days=1)))

        first = generate_overdue(self.ho)
        again = generate_overdue(self.ho)

        self.assertEqual(first, 2)  # the assignee and the owner who assigned it
        self.assertEqual(again, 0)
        self.assertEqual(Notification.objects.filter(kind="task_overdue").count(), 2)

    # -- notifications are personal ------------------------------------

    def test_reading_a_task_notification_is_only_for_me(self):
        self.assign()
        self.as_user("ram")
        note = self.notes()[0]

        self.client.post(f"{NOTES}{note['id']}/read/")

        self.assertTrue(self.notes()[0]["is_read"])
        self.assertEqual(self.client.get(NOTES + "unread-count/").json()["unread"], 0)


class RepeatAndAttachmentTests(TaskTests):
    """Repeating tasks and files kept on a task (inherits the setup)."""

    def setUp(self):
        super().setUp()
        import tempfile
        from django.test import override_settings

        self._media = tempfile.TemporaryDirectory()
        override = override_settings(MEDIA_ROOT=self._media.name)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(self._media.cleanup)

    def done(self, task):
        return self.client.post(f"{TASKS}{task}/status/", {"status": "done"}, format="json")

    def test_a_repeating_task_needs_a_due_date(self):
        self.assertEqual(self.assign(repeat="weekly").status_code, 400)

    def test_finishing_a_repeating_task_makes_the_next_one_once(self):
        today = timezone.localdate()
        task = self.assign(repeat="weekly", due_date=str(today + timedelta(days=1))).json()["id"]
        self.as_user("ram")

        self.assertEqual(self.done(task).status_code, 200)
        self.client.post(f"{TASKS}{task}/status/", {"status": "todo"}, format="json")
        self.done(task)

        later = Task.objects.exclude(pk=task)
        self.assertEqual(later.count(), 1)
        nxt = later.get()
        self.assertEqual(nxt.due_date, today + timedelta(days=8))
        self.assertEqual((nxt.repeat, nxt.status, nxt.assigned_to), ("weekly", "todo", self.ram))

    def test_a_late_repeating_task_never_gets_a_past_due_date(self):
        today = timezone.localdate()
        task = self.assign(repeat="daily", due_date=str(today - timedelta(days=5))).json()["id"]
        self.as_user("ram")
        self.done(task)

        self.assertEqual(Task.objects.exclude(pk=task).get().due_date, today)

    def test_monthly_repeat_keeps_to_the_end_of_short_months(self):
        from datetime import date
        from apps.tasks.services import next_due

        self.assertEqual(next_due(date(2026, 1, 31), "monthly", date(2026, 1, 1)), date(2026, 2, 28))
        self.assertEqual(next_due(date(2026, 12, 15), "monthly", date(2026, 1, 1)), date(2027, 1, 15))

    def test_a_task_that_does_not_repeat_makes_nothing(self):
        task = self.assign(due_date=str(timezone.localdate())).json()["id"]
        self.as_user("ram")
        self.done(task)

        self.assertEqual(Task.objects.count(), 1)

    def attach(self, task, name="bill.pdf", content=b"%PDF-1"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return self.client.post(
            f"{TASKS}{task}/attachments/",
            {"file": SimpleUploadedFile(name, content)},
            format="multipart",
        )

    def test_the_assignee_adds_downloads_and_the_owner_removes_a_file(self):
        task = self.assign().json()["id"]
        self.as_user("ram")

        response = self.attach(task)
        self.assertEqual(response.status_code, 201, response.content)
        item = response.json()["id"]
        self.assertEqual(self.client.get(f"{TASKS}{task}/").json()["attachment_count"], 1)

        download = self.client.get(f"{TASKS}{task}/attachments/{item}/")
        self.assertEqual(b"".join(download.streaming_content), b"%PDF-1")

        self.authenticate("ACME-HO", "owner")
        self.assertEqual(self.client.delete(f"{TASKS}{task}/attachments/{item}/").status_code, 204)
        self.assertEqual(self.client.get(f"{TASKS}{task}/attachments/").json(), [])

    def test_someone_not_on_the_task_cannot_add_or_see_files(self):
        task = self.assign().json()["id"]
        self.as_user("ram")
        item = self.attach(task).json()["id"]

        self.as_user("sita")
        self.assertEqual(self.client.get(f"{TASKS}{task}/attachments/").status_code, 404)
        self.assertEqual(self.attach(task).status_code, 404)
        self.assertEqual(self.client.get(f"{TASKS}{task}/attachments/{item}/").status_code, 404)

    def test_only_safe_types_and_sizes(self):
        task = self.assign().json()["id"]

        self.assertEqual(self.attach(task, name="x.exe").status_code, 400)
        self.assertEqual(self.attach(task, content=b"x" * (10 * 1024 * 1024 + 1)).status_code, 400)
