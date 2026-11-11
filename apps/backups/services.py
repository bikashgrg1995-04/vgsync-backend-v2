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
