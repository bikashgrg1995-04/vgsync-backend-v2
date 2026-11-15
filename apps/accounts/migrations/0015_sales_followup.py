from django.db import migrations


def grant_followup(apps, schema_editor):
    """Working a follow-up became its own permission; whoever could edit or
    create sales (the only ones who could before) keeps it."""
    Permission = apps.get_model("accounts", "PositionPermission")

    for row in Permission.objects.filter(module="sales"):
        if not (row.can_create or row.can_update):
            continue

        actions = list(row.actions or [])

        if "followup" not in actions:
            row.actions = sorted(actions + ["followup"])
            row.save(update_fields=["actions"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0014_inventory_view_cost")]
    operations = [migrations.RunPython(grant_followup, migrations.RunPython.noop)]
