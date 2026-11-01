from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payroll", "0004_convert_profiles"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="salaryprofile",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("allowances__gte", 0),
                    models.Q(("rate__isnull", True), ("rate__gte", 0), _connector="OR"),
                ),
                name="salary_profile_amounts_not_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="salaryprofile",
            constraint=models.CheckConstraint(
                condition=models.Q(("salary_day__gte", 1), ("salary_day__lte", 31)),
                name="salary_profile_day_in_month",
            ),
        ),
        migrations.AddConstraint(
            model_name="salaryprofile",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("pay_type", "monthly"), ("rate__isnull", False)),
                    models.Q(("pay_type", "daily"), ("rate__isnull", True)),
                    _connector="OR",
                ),
                name="salary_profile_rate_matches_pay_type",
            ),
        ),
    ]
