import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0006_convert_admin_accountant_roles"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="role",
            field=models.CharField(
                choices=[
                    ("super_admin", "Super Admin"),
                    ("owner", "Owner"),
                    ("staff", "Staff"),
                ],
                default="staff",
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("business__isnull", True),
                        ("company__isnull", True),
                        ("role", "super_admin"),
                    ),
                    models.Q(
                        ("business__isnull", True),
                        ("company__isnull", False),
                        ("role__in", ["owner", "staff"]),
                    ),
                    models.Q(
                        ("business__isnull", False),
                        ("company__isnull", True),
                        ("role__in", ["owner", "staff"]),
                    ),
                    _connector="OR",
                ),
                name="valid_user_role_scope",
            ),
        ),
    ]
