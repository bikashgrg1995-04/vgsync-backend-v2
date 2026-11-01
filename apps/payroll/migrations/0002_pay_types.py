from django.db import migrations, models
from django.db.models import F, Q


def copy_basic_to_rate(apps, schema_editor):
    """Existing records were all monthly: their basic salary is the rate."""
    PayrollRecord = apps.get_model("payroll", "PayrollRecord")
    PayrollRecord.objects.update(rate=F("basic_salary"))


class Migration(migrations.Migration):

    dependencies = [
        ("payroll", "0001_initial"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="salaryprofile",
            name="salary_profile_amounts_not_negative",
        ),
        migrations.RemoveConstraint(
            model_name="payrollrecord",
            name="payroll_amounts_not_negative",
        ),
        migrations.RenameField(
            model_name="salaryprofile",
            old_name="basic_salary",
            new_name="rate",
        ),
        migrations.AddField(
            model_name="salaryprofile",
            name="pay_type",
            field=models.CharField(
                choices=[("monthly", "Monthly"), ("daily", "Daily"), ("hourly", "Hourly")],
                default="monthly",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="payrollrecord",
            name="pay_type",
            field=models.CharField(
                choices=[("monthly", "Monthly"), ("daily", "Daily"), ("hourly", "Hourly")],
                default="monthly",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="payrollrecord",
            name="rate",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name="payrollrecord",
            name="units",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=7, null=True),
        ),
        migrations.AlterField(
            model_name="payrollrecord",
            name="basic_salary",
            field=models.DecimalField(decimal_places=2, default=0, editable=False, max_digits=12),
        ),
        migrations.RunPython(copy_basic_to_rate, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="salaryprofile",
            constraint=models.CheckConstraint(
                condition=Q(rate__gte=0) & Q(allowances__gte=0),
                name="salary_profile_amounts_not_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="payrollrecord",
            constraint=models.CheckConstraint(
                condition=(
                    Q(rate__gte=0)
                    & Q(basic_salary__gte=0)
                    & Q(allowances__gte=0)
                    & Q(bonus__gte=0)
                    & Q(deductions__gte=0)
                    & Q(advance_deduction__gte=0)
                    & Q(net_salary__gte=0)
                ),
                name="payroll_amounts_not_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="payrollrecord",
            constraint=models.CheckConstraint(
                condition=Q(units__isnull=True) | Q(units__gte=0),
                name="payroll_units_not_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="payrollrecord",
            constraint=models.CheckConstraint(
                condition=Q(units__isnull=True) | ~Q(pay_type="monthly"),
                name="payroll_monthly_has_no_units",
            ),
        ),
    ]
