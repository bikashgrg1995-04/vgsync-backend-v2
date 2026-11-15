from django.db import migrations


def grant_view_all(apps, schema_editor):
    """Sales became visible per salesperson; keep everyone who could see
    sales able to see all of them, as before."""
    Permission = apps.get_model("accounts", "PositionPermission")

    for row in Permission.objects.filter(module="sales", can_view=True):
        actions = list(row.actions or [])

        if "view_all" not in actions:
            row.actions = sorted(actions + ["view_all"])
            row.save(update_fields=["actions"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0011_staff_document")]
    operations = [migrations.RunPython(grant_view_all, migrations.RunPython.noop)]
