"""
Fix products that were imported from the old system with the name stuck on the
stock number ("36030072 Spacer" / "- 4.057 mm" becomes "36030072" / "Spacer - 4.057 mm").

    python manage.py repair_legacy_stock_numbers --business IND-LEG          # preview
    python manage.py repair_legacy_stock_numbers --business IND-LEG --apply  # save
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.backups import stock_labels
from apps.businesses.models import Business
from apps.inventory.models import Product


class Command(BaseCommand):
    help = "Separate stock numbers that have the item name stuck on the end."

    def add_arguments(self, parser):
        parser.add_argument("--business", required=True, help="Business code")
        parser.add_argument("--apply", action="store_true", help="Save the changes")

    def handle(self, *args, business, apply, **options):
        try:
            owner = Business.objects.get(business_code=business)
        except Business.DoesNotExist:
            raise CommandError(f"No business with code {business}.")

        products = list(Product.objects.filter(owner_business=owner))
        words = stock_labels.vocabulary((p.stock_no, p.name) for p in products)
        taken = {p.stock_no.lower() for p in products}
        changes, clashes = [], []

        for p in products:
            if not stock_labels.is_split(p.stock_no):
                continue

            number, name = stock_labels.fix_label(p.stock_no, p.name, words)

            if number.lower() in taken and number.lower() != p.stock_no.lower():
                clashes.append((p.stock_no, number))
                continue

            taken.discard(p.stock_no.lower())
            taken.add(number.lower())
            changes.append((p, number, name))

        for p, number, name in changes[:15]:
            self.stdout.write(f"  {p.stock_no!r} | {p.name!r}  ->  {number!r} | {name!r}")

        self.stdout.write(f"{len(changes)} products to fix, {len(clashes)} skipped (new number already used).")

        for old, new in clashes[:10]:
            self.stdout.write(f"  skipped {old!r}: {new!r} is already another product")

        if not apply:
            self.stdout.write("Preview only. Add --apply to save.")
            return

        with transaction.atomic():
            for p, number, name in changes:
                p.stock_no, p.name = number[:100], name[:200]
                p.save(update_fields=["stock_no", "name", "updated_at"] if hasattr(p, "updated_at") else ["stock_no", "name"])

        self.stdout.write(self.style.SUCCESS(f"Fixed {len(changes)} products."))
