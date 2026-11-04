from django.core.management.base import BaseCommand

from apps.notifications.services import generate_everywhere


class Command(BaseCommand):
    help = (
        "Make notifications for follow-ups that are due or overdue, in every "
        "active business. Safe to run as often as you like; run it daily."
    )

    def handle(self, *args, **options):
        made = generate_everywhere()
        self.stdout.write(f"{made} notification(s) made.")
