from django.db import migrations

# Admin and Accountant stop being roles: each such user becomes Staff holding
# a Position (created if needed) that carries the matching preset. The
# Accountant preset is narrower than the old blanket Admin-like access;
# owners can widen it afterwards.
PRESET_FOR_ROLE = {"admin": "administrator", "accountant": "accountant"}


def convert(apps, schema_editor):
    from apps.accounts import rbac

    User = apps.get_model("accounts", "User")
    Position = apps.get_model("accounts", "Position")
    PositionPermission = apps.get_model("accounts", "PositionPermission")
    Business = apps.get_model("businesses", "Business")

    for user in User.objects.filter(role__in=PRESET_FOR_ROLE):
        key = PRESET_FOR_ROLE[user.role]
        preset = rbac.PRESETS[key]

        if user.company_id:
            business = Business.objects.filter(
                company_id=user.company_id,
                business_type="head_office",
            ).first()
        else:
            business = Business.objects.filter(pk=user.business_id).first()

        if business is None:
            continue

        position = Position.objects.filter(
            business=business, name__iexact=preset["label"]
        ).first()

        if position is None:
            position = Position.objects.create(
                business=business,
                name=preset["label"],
                department=preset["department"],
                preset=key,
                description="Created from the former %s role." % user.role,
            )

            for module, actions in preset["grants"].items():
                PositionPermission.objects.create(
                    position=position,
                    module=module,
                    can_view="view" in actions,
                    can_create="create" in actions,
                    can_update="update" in actions,
                    can_delete="delete" in actions,
                    actions=[
                        a for a in actions
                        if a not in ("view", "create", "update", "delete")
                    ],
                )

        user.role = "staff"
        user.position = position
        user.save(update_fields=["role", "position"])


# Before this change, creating anything meant "create". The finer actions
# that now exist keep working for Positions that could already create.
CARRY_OVER = {
    "purchases": ["receive", "pay", "return"],
    "sales": ["cancel"],
    "inventory": ["adjust", "write_off", "transfer"],
    "finance": ["void"],
}


def carry_over(apps, schema_editor):
    PositionPermission = apps.get_model("accounts", "PositionPermission")

    for row in PositionPermission.objects.all():
        extra = CARRY_OVER.get(row.module, []) if row.can_create else []

        if row.module == "staff" and row.can_update:
            extra = ["assign_position"]

        if extra:
            row.actions = sorted(set(row.actions or []) | set(extra))
            row.save(update_fields=["actions"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_rbac_overrides_and_positions_for_admin"),
        ("businesses", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(carry_over, migrations.RunPython.noop),
        migrations.RunPython(convert, migrations.RunPython.noop),
    ]
