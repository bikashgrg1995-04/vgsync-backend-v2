from django.db import migrations


def convert_profiles(apps, schema_editor):
    """
    Hourly pay is gone: those people become daily (no fixed rate), and
    daily people carry no allowances. Monthly salary is recorded from today
    on; the old per-period records are not carried over.
    """
    SalaryProfile = apps.get_model("payroll", "SalaryProfile")

    SalaryProfile.objects.filter(pay_type="hourly").update(pay_type="daily")
    SalaryProfile.objects.filter(pay_type="daily").update(
        rate=None, allowances=0
    )


class Migration(migrations.Migration):
    # Data changes and constraint changes cannot share a transaction on
    # PostgreSQL, so this step stands alone.

    dependencies = [
        ("payroll", "0003_pay_ledger"),
    ]

    operations = [
        migrations.RunPython(convert_profiles, migrations.RunPython.noop),
    ]
