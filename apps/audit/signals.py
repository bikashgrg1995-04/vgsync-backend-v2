"""
Writes the audit log. A row is added whenever a tracked model is created,
changed or deleted while a request is running. Rows are written inside the
same transaction as the change, so a rolled back change leaves no trace.
"""

import datetime
import decimal

from django.apps import apps as django_apps
from django.db.models.signals import post_delete, post_save, pre_save

from . import context
from .models import AuditLog

# Never recorded: noise, or secrets.
SKIP_FIELDS = {"created_at", "updated_at", "password", "last_login"}

# model label -> path to the business the row belongs to, tried in order.
BUSINESS_PATHS = (
    "business_id",
    "owner_business_id",
    "purchase.business_id",
    "sale.business_id",
    "supplier.business_id",
    "stock_balance.business_id",
    "position.business_id",
    "user.business_id",
)

# Models to track. The value says which events to record:
# ``True`` for all, or a function that picks the rows worth recording.
TRACKED = {
    "inventory.Product": True,
    "inventory.LocalProduct": True,
    "inventory.StockMovement": lambda movement: movement.movement_type
    in ("adjustment_in", "adjustment_out", "opening"),
    "purchases.Supplier": True,
    "purchases.Purchase": True,
    "purchases.PurchasePayment": True,
    "purchases.PurchaseReturn": True,
    "purchases.CreditSettlement": True,
    "sales.Customer": True,
    "sales.BikeUnit": True,
    "sales.Sale": True,
    "sales.SalePayment": True,
    "sales.SaleReturn": True,
    "sales.SaleCreditSettlement": True,
    "finance.Expense": True,
    "finance.Income": True,
    "payroll.PayrollPayment": True,
    "payroll.SalaryProfile": True,
    "accounts.User": True,
    "accounts.Position": True,
    "businesses.Business": True,
    "accounts.PositionPermission": True,
    "accounts.StaffPermissionOverride": True,
    "approvals.ApprovalRule": True,
    "approvals.ApprovalRequest": True,
}


def _label(sender):
    return f"{sender._meta.app_label}.{sender.__name__}"


def _tracked(instance):
    rule = TRACKED.get(_label(type(instance)))

    if rule is None:
        return False

    return True if rule is True else bool(rule(instance))


def _safe(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    if isinstance(value, decimal.Decimal):
        return f"{value:f}"

    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()

    return str(value)


def _values(instance):
    return {
        field.attname: _safe(getattr(instance, field.attname))
        for field in instance._meta.concrete_fields
        if field.name not in SKIP_FIELDS
    }


def _business_id(instance, state):
    if instance._meta.label == "businesses.Business":
        return instance.pk

    for path in BUSINESS_PATHS:
        value = instance

        try:
            for part in path.split("."):
                value = getattr(value, part)
        except Exception:
            continue

        if value:
            return value

    business = state.get("business")

    return business.pk if business else None


def _actor_name(actor):
    """Full name when the person has one, otherwise the login name."""
    if actor is None:
        return ""

    full = f"{getattr(actor, 'first_name', '')} {getattr(actor, 'last_name', '')}".strip()

    return full or getattr(actor, "username", "") or ""


def _write(instance, action, changes):
    state = context.current()

    if state is None:
        return

    actor = state.get("actor")

    AuditLog.objects.create(
        business_id=_business_id(instance, state),
        actor=actor,
        actor_name=_actor_name(actor),
        action=action,
        model=instance._meta.label_lower,
        object_id=str(instance.pk),
        object_repr=str(instance)[:200],
        changes=changes,
    )


def remember_old(sender, instance, raw=False, **kwargs):
    if raw or context.current() is None or not _tracked(instance):
        return

    instance._audit_old = None

    if instance.pk is not None:
        row = sender._default_manager.filter(pk=instance.pk).first()

        if row is not None:
            instance._audit_old = _values(row)


def log_save(sender, instance, created, raw=False, **kwargs):
    if raw or context.current() is None or not _tracked(instance):
        return

    new = _values(instance)

    if created:
        _write(instance, AuditLog.Action.CREATE, new)

        return

    old = getattr(instance, "_audit_old", None) or {}
    changes = {
        name: [old.get(name), value]
        for name, value in new.items()
        if name in old and old[name] != value
    }

    if changes:
        _write(instance, AuditLog.Action.UPDATE, changes)


def log_delete(sender, instance, **kwargs):
    if context.current() is None or not _tracked(instance):
        return

    _write(instance, AuditLog.Action.DELETE, _values(instance))


def _connect():
    for label in TRACKED:
        model = django_apps.get_model(label)
        uid = f"audit-{label}"

        pre_save.connect(remember_old, sender=model, dispatch_uid=f"{uid}-pre")
        post_save.connect(log_save, sender=model, dispatch_uid=f"{uid}-post")
        post_delete.connect(log_delete, sender=model, dispatch_uid=f"{uid}-del")


_connect()
