import ast

from rest_framework import serializers

from .models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    summary = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = (
            "id",
            "created_at",
            "actor",
            "actor_name",
            "action",
            "model",
            "object_id",
            "object_repr",
            "summary",
            "changes",
        )
        read_only_fields = fields

    VERBS = {
        AuditLog.Action.CREATE: "Added",
        AuditLog.Action.DELETE: "Removed",
    }

    # Access records say what happened in their own words.
    MODEL_VERBS = {
        "accounts.staffpermissionoverride": ("Override set:", "Override cleared:"),
        "accounts.positionpermission": ("Access added:", "Access removed:"),
        "accounts.position": ("Position added:", "Position removed:"),
        "approvals.approvalrule": ("Rule added:", "Rule removed:"),
    }

    # These changes only make sense with the record they belong to.
    NAMED_MODELS = frozenset(MODEL_VERBS) | {"approvals.approvalrequest"}

    # A foreign key shown by name instead of a number.
    NAMED_FIELDS = {"position_id": "accounts.position", "user_id": "accounts.user"}

    def get_summary(self, obj):
        """
        One line a person can read. A change lists each field as
        ``amount: 500.00 -> 600.00``; a list field says what was added and
        removed; a yes/no field says yes or no.
        """
        if obj.action != AuditLog.Action.UPDATE:
            created, removed = self.MODEL_VERBS.get(
                obj.model, (self.VERBS[AuditLog.Action.CREATE], self.VERBS[AuditLog.Action.DELETE])
            )
            verb = created if obj.action == AuditLog.Action.CREATE else removed

            return f"{verb} {obj.object_repr}"

        text = "; ".join(
            self._change(name, old, new, self._name) for name, (old, new) in obj.changes.items()
        )

        return f"{obj.object_repr}: {text}" if obj.model in self.NAMED_MODELS else text

    def _name(self, field, value):
        """A position or person's name for a stored id; the id if it is gone."""
        label = self.NAMED_FIELDS.get(field)

        if not label or value in (None, ""):
            return value

        cache = self.context.setdefault("_names", {})
        key = (label, str(value))

        if key not in cache:
            from django.apps import apps

            row = apps.get_model(label).objects.filter(pk=value).first()

            if row is None:
                cache[key] = value
            elif label == "accounts.user":
                cache[key] = row.get_full_name() or row.username
            else:
                cache[key] = row.name

        return cache[key]

    @staticmethod
    def _items(value):
        """A list stored as text (``"['a', 'b']"``) or as a real list."""
        if isinstance(value, list):
            return value

        if isinstance(value, str) and value.startswith("["):
            try:
                found = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return None

            return found if isinstance(found, list) else None

        return None

    @staticmethod
    def _shown(value):
        if value is True:
            return "yes"

        if value is False:
            return "no"

        return "none" if value in (None, "") else value

    @classmethod
    def _change(cls, name, old, new, lookup=None):
        label = (name[:-3] if name.endswith("_id") else name).replace("_", " ")

        if lookup is not None:
            old, new = lookup(name, old), lookup(name, new)

        before, after = cls._items(old), cls._items(new)

        if before is not None and after is not None:
            added = [str(x) for x in after if x not in before]
            removed = [str(x) for x in before if x not in after]
            parts = []

            if added:
                parts.append(f"added {', '.join(added)}")

            if removed:
                parts.append(f"removed {', '.join(removed)}")

            return f"{label}: {' and '.join(parts) or 'no change'}"

        return f"{label}: {cls._shown(old)} -> {cls._shown(new)}"
