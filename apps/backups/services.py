"""
Builds a backup: a ZIP with one CSV per table plus ``manifest.json``.

Every table that belongs to a business (directly, or through a parent such
as a sale line to its sale) is included, limited to the businesses in
scope. Passwords and tokens are never written.
"""

import csv
import io
import json
import zipfile
from datetime import datetime, timezone

from django.apps import apps as django_apps
from django.db import models

FORMAT_VERSION = 1
SKIP_APPS = {"contenttypes", "sessions", "admin", "auth", "backups"}
SKIP_FIELD_WORDS = ("password", "token", "secret", "otp")
SKIP_MODEL_WORDS = ("token", "session", "otp")


def businesses_in_scope(user, business):
    """The company owner's backup covers the whole company."""
    from apps.businesses.models import Business

    if business.company_id and getattr(user, "company_id", None):
        return list(Business.objects.filter(company_id=business.company_id))

    return [business]


BUSINESS = "businesses.Business"
# Foreign keys that mean "belongs to this business".
OWNER_FIELDS = ("business", "owner_business", "source_business", "destination_business")
# Foreign keys never followed when looking for the owning business: who did
# it, and catalogue rows that are shared across businesses.
NOT_PARENTS = {"actor", "product", "transfer_item", "receipt_item", "purchase_item"}


def _links(field):
    return (field.many_to_one or field.one_to_one) and field.related_model is not None


def _business_paths(model, depth=0):
    """Lookups that reach the owning business(es) from this model."""
    if model._meta.label == BUSINESS:
        return ["pk"]

    fks = [f for f in model._meta.concrete_fields if _links(f)]
    direct = [
        f.name
        for f in fks
        if f.name in OWNER_FIELDS and f.related_model._meta.label == BUSINESS
    ]

    if direct:
        return direct

    if depth >= 3:
        return []

    # Climb through parents, for example a sale line to its sale.
    paths = []

    for f in fks:
        if (
            f.name in NOT_PARENTS
            or f.name.endswith("_by")
            or f.related_model._meta.label == BUSINESS
        ):
            continue

        for up in _business_paths(f.related_model, depth + 1):
            if up != "pk":
                paths.append(f"{f.name}__{up}")

    return paths


def backup_models():
    found = []

    for model in django_apps.get_models():
        if model._meta.app_label in SKIP_APPS or model._meta.proxy:
            continue

        if any(w in model._meta.model_name for w in SKIP_MODEL_WORDS):
            continue

        paths = _business_paths(model)

        if paths:
            found.append((model, paths))

    return sorted(found, key=lambda x: x[0]._meta.label)


def _cell(value):
    if value is None:
        return ""

    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)

    if isinstance(value, datetime):
        return value.isoformat()

    return value


def build_backup(user, business):
    """Return ``(zip_bytes, summary)``."""
    scope_businesses = businesses_in_scope(user, business)
    ids = [b.pk for b in scope_businesses]
    company_id = (
        business.company_id if business.company_id and user.company_id else None
    )
    buffer = io.BytesIO()
    tables = []
    total_rows = 0

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for model, paths in backup_models():
            where = models.Q()

            for path in paths:
                where |= models.Q(**{("pk__in" if path == "pk" else f"{path}__in"): ids})

            fields = [
                f
                for f in model._meta.concrete_fields
                if not any(w in f.name for w in SKIP_FIELD_WORDS)
            ]
            if model._meta.label == "accounts.User" and company_id:
                # The company owner belongs to the company, not a business.
                where |= models.Q(company_id=company_id)

            queryset = model._default_manager.filter(where).distinct().order_by("pk")

            if not queryset.exists():
                continue

            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow([f.attname for f in fields])
            rows = 0

            for obj in queryset.iterator():
                writer.writerow([_cell(getattr(obj, f.attname)) for f in fields])
                rows += 1

            archive.writestr(f"{model._meta.label}.csv", "﻿" + out.getvalue())
            tables.append({"table": model._meta.label, "rows": rows})
            total_rows += rows

        manifest = {
            "format_version": FORMAT_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": user.get_full_name() or user.username,
            "scope": "company" if len(scope_businesses) > 1 or (
                business.company_id and getattr(user, "company_id", None)
            ) else "business",
            "businesses": [
                {"id": b.pk, "code": b.business_code, "name": b.name}
                for b in scope_businesses
            ],
            "tables": tables,
            "note": "Passwords and login tokens are not included.",
        }
        archive.writestr(
            "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False)
        )

    data = buffer.getvalue()
    summary = {
        "scope": manifest["scope"],
        "business_count": len(scope_businesses),
        "file_count": len(tables),
        "row_count": total_rows,
        "size_bytes": len(data),
    }

    return data, summary


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

# Never restored, on purpose:
# - sign-in accounts, positions and permissions: passwords are not in a
#   backup, and putting an old list of staff or access back could reactivate
#   someone who has since left or hand back permissions that were taken away;
# - the change history (audit): history is never rewound;
# - notifications: they are rebuilt from the restored records (see
#   ``apply_restore``) instead of bringing back stale ones;
# - the businesses themselves and stock transfers between businesses (they
#   involve other businesses' data), and support tickets.
RESTORE_SKIP_APPS = {"accounts", "audit", "notifications", "businesses", "support"}
RESTORE_SKIP_WORDS = ("transfer",)


class RestoreError(Exception):
    pass


def restorable_models():
    """``(model, lookup)`` pairs a restore replaces, one lookup each."""
    found = []

    for model, paths in backup_models():
        if model._meta.app_label in RESTORE_SKIP_APPS:
            continue

        if any(w in model._meta.model_name for w in RESTORE_SKIP_WORDS):
            continue

        found.append((model, paths[0]))

    return found


def read_backup(data, scope_ids):
    """Open an uploaded ZIP and check it belongs to this scope."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        manifest = json.loads(archive.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, ValueError):
        raise RestoreError("This is not a VGSync backup file.")

    if manifest.get("format_version") != FORMAT_VERSION:
        raise RestoreError("This backup was made by a different version and cannot be restored.")

    in_file = sorted(b["id"] for b in manifest.get("businesses", []))

    if in_file != sorted(scope_ids):
        raise RestoreError(
            "This backup was taken from a different business. "
            "Restore it from the business it came from."
        )

    return archive, manifest


def _value(field, raw):
    if field.get_internal_type() == "JSONField":
        return json.loads(raw) if raw != "" else None

    if raw == "":
        if field.null:
            return None

        if field.get_internal_type() in ("CharField", "TextField", "SlugField", "EmailField"):
            return ""

        return None

    return field.to_python(raw)


def _rows_from_csv(archive, model):
    name = f"{model._meta.label}.csv"

    if name not in archive.namelist():
        return []

    text = archive.read(name).decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, [])
    by_attname = {f.attname: f for f in model._meta.concrete_fields}
    columns = [by_attname.get(h) for h in header]
    objects = []

    for line in reader:
        obj = model()

        for field, raw in zip(columns, line):
            if field is not None:
                setattr(obj, field.attname, _value(field, raw))

        objects.append(obj)

    return objects


def _restorable_rows(archive, model):
    """Rows of the backup that belong to the businesses being restored."""
    rows = _rows_from_csv(archive, model)

    if model._meta.label == "inventory.StockMovement":
        # The backup also holds transfer movements that sit in another
        # business's stock; those are not this restore's to replace.
        from apps.inventory.models import StockBalance

        mine = {b.pk for b in _rows_from_csv(archive, StockBalance)}
        rows = [r for r in rows if r.stock_balance_id in mine]

    return rows


def preview_restore(user, business, data):
    scope_ids = [b.pk for b in businesses_in_scope(user, business)]
    archive, manifest = read_backup(data, scope_ids)
    tables = []

    for model, path in restorable_models():
        lookup = "pk__in" if path == "pk" else f"{path}__in"
        current = model._default_manager.filter(**{lookup: scope_ids}).count()
        in_backup = len(_restorable_rows(archive, model))

        if current or in_backup:
            tables.append(
                {"table": model._meta.label, "now": current, "backup": in_backup}
            )

    return {
        "created_at": manifest.get("created_at"),
        "created_by": manifest.get("created_by"),
        "scope": manifest.get("scope"),
        "businesses": manifest.get("businesses", []),
        "tables": tables,
        "backup_rows": sum(t["backup"] for t in tables),
        "current_rows": sum(t["now"] for t in tables),
    }


def apply_restore(user, business, data):
    """
    Replace the business's data with the backup, all or nothing. Returns the
    number of records restored.
    """
    from django.db import IntegrityError, connection, transaction

    scope_ids = [b.pk for b in businesses_in_scope(user, business)]
    archive, _ = read_backup(data, scope_ids)
    plan = [(m, p, _restorable_rows(archive, m)) for m, p in restorable_models()]
    touched = [f for m, _, _ in plan for f in m._meta.concrete_fields if getattr(f, "auto_now", False) or getattr(f, "auto_now_add", False)]
    saved = [(f, f.auto_now, f.auto_now_add) for f in touched]
    restored = 0

    # Keep the stored timestamps instead of stamping "now".
    for field in touched:
        field.auto_now = False
        field.auto_now_add = False

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL DEFERRED")

            # Find every row to remove first: deleting a parent table would
            # otherwise hide its children from the lookups.
            # Notifications point at follow-ups, purchases and tasks that
            # may not exist in the backup, so clear them first; they are
            # rebuilt from the restored records afterwards.
            from apps.notifications.models import Notification

            Notification.objects.filter(business_id__in=scope_ids).delete()

            doomed = []

            for model, path, _ in plan:
                lookup = "pk__in" if path == "pk" else f"{path}__in"
                doomed.append(
                    (
                        model,
                        list(
                            model._base_manager.filter(**{lookup: scope_ids})
                            .values_list("pk", flat=True)
                        ),
                    )
                )

            for model, pks in doomed:
                for start in range(0, len(pks), 1000):
                    query = model._base_manager.filter(pk__in=pks[start:start + 1000])
                    query._raw_delete(query.db)

            for model, _, objects in plan:
                if objects:
                    models.QuerySet(model).bulk_create(objects, batch_size=500)
                    restored += len(objects)

            # Rows were inserted with their own ids: move the counters on.
            statements = connection.ops.sequence_reset_sql(
                _no_style(), [m for m, _, _ in plan]
            )

            with connection.cursor() as cursor:
                for sql in statements:
                    cursor.execute(sql)

            # Surface any broken link now, while the whole restore can still
            # be undone.
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    except IntegrityError:
        raise RestoreError(
            "Some records made after this backup depend on newer data, so it "
            "cannot be restored over them. Nothing was changed."
        )
    finally:
        for field, auto_now, auto_now_add in saved:
            field.auto_now = auto_now
            field.auto_now_add = auto_now_add

    return restored


def _no_style():
    from django.core.management.color import no_style

    return no_style()
