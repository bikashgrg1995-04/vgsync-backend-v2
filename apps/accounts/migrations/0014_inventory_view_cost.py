from django.db import migrations


def grant_view_cost(apps, schema_editor):
    """Cost prices became their own permission; whoever could see stock
    keeps seeing them, as before (the owner can then take it off a position,
    for instance a technician's)."""
    Permission = apps.get_model("accounts", "PositionPermission")

    for row in Permission.objects.filter(module="inventory", can_view=True):
        actions = list(row.actions or [])

        if "view_cost" not in actions:
            row.actions = sorted(actions + ["view_cost"])
            row.save(update_fields=["actions"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0013_user_extra_positions")]
    operations = [migrations.RunPython(grant_view_cost, migrations.RunPython.noop)]
