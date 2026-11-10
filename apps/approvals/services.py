"""
The approval gate. A view calls ``gate`` before doing a risky operation:

* No active rule, or an amount under the threshold: nothing happens.
* The owner is never held (the owner is the approver of last resort).
* Otherwise the request is held (409 ``approval_required``) until a person
  with the matching ``*.approve`` permission approves it, and the client
  repeats the same request with ``approval_id``.
"""

import hashlib
import json
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import (
    APIException,
    PermissionDenied,
    ValidationError,
)

from apps.accounts import rbac
from apps.accounts.models import User
from apps.common.exceptions import Conflict

from .models import APPROVER_PERMISSION, ApprovalRequest, ApprovalRule


def fingerprint(payload):
    """Stable hash of what was asked for (``approval_id`` excluded)."""
    clean = {k: v for k, v in payload.items() if k != "approval_id"}
    text = json.dumps(clean, sort_keys=True, default=str)

    return hashlib.sha256(text.encode()).hexdigest()


def _plain(data):
    return json.loads(json.dumps(dict(data), default=str))


def needs_approval(business, kind, amount):
    rule = ApprovalRule.objects.filter(
        business=business, kind=kind, is_active=True
    ).first()

    if rule is None:
        return None

    return rule if Decimal(amount) >= rule.threshold else None


def gate(request, kind, amount, summary=""):
    """Hold or release the operation in ``request``. Returns the used approval
    (or None when no approval was needed)."""
    user = request.user
    business = request.active_business

    if user.role == User.Role.OWNER:
        return None

    if needs_approval(business, kind, amount) is None:
        return None

    payload = _plain(request.data)
    digest = fingerprint(payload)
    approval_id = request.data.get("approval_id")

    if approval_id:
        return _use_approval(approval_id, business, kind, user, digest)

    approval = ApprovalRequest.objects.filter(
        business=business,
        kind=kind,
        created_by=user,
        payload_hash=digest,
        status=ApprovalRequest.Status.PENDING,
    ).first()

    if approval is None:
        approval = ApprovalRequest.objects.create(
            business=business,
            kind=kind,
            amount=amount,
            summary=summary[:200],
            payload=payload,
            payload_hash=digest,
            created_by=user,
        )

    raise ApprovalRequired(approval)


@transaction.atomic
def _use_approval(approval_id, business, kind, user, digest):
    """Spend an approved request once. Raises if it does not fit."""
    approval = (
        ApprovalRequest.objects.select_for_update()
        .filter(pk=approval_id, business=business, kind=kind)
        .first()
    )

    if (
        approval is None
        or approval.created_by_id != user.pk
        or approval.payload_hash != digest
    ):
        raise ValidationError(
            {"approval_id": "This approval does not match this request."}
        )

    if approval.status == ApprovalRequest.Status.REJECTED:
        raise Conflict("This request was rejected.", code="approval_rejected")

    if approval.status != ApprovalRequest.Status.APPROVED:
        raise Conflict(
            "This request is still waiting for approval.",
            code="approval_pending",
        )

    if approval.used_at is not None:
        raise Conflict(
            "This approval has already been used.", code="approval_used"
        )

    approval.used_at = timezone.now()
    approval.save(update_fields=["used_at"])

    return approval


class ApprovalRequired(APIException):
    """409 with the id of the held request, so the client can wait on it."""

    status_code = status.HTTP_409_CONFLICT
    default_code = "approval_required"

    def __init__(self, approval):
        super().__init__(
            detail={
                "detail": "This needs approval before it can go ahead.",
                "code": self.default_code,
                "approval_id": approval.pk,
            }
        )


def decide(approval, user, approve, access_mode="full", reason=""):
    """Approve or reject a pending request, with the safety rules."""
    if access_mode != "full":
        raise PermissionDenied("Approvals cannot be decided in read-only mode.")

    if approval.status != ApprovalRequest.Status.PENDING:
        raise Conflict("This request was already decided.", code="already_decided")

    allowed = user.role == User.Role.OWNER or rbac.has_permission(
        user, APPROVER_PERMISSION[approval.kind], access_mode
    )

    if not allowed:
        raise PermissionDenied("You cannot approve this kind of request.")

    if approval.created_by_id == user.pk and user.role != User.Role.OWNER:
        rule = ApprovalRule.objects.filter(
            business=approval.business, kind=approval.kind
        ).first()

        if rule is None or not rule.allow_self_approval:
            raise PermissionDenied("You cannot approve your own request.")

    if not approve and not reason.strip():
        raise ValidationError({"reason": "Say why it is rejected."})

    approval.status = (
        ApprovalRequest.Status.APPROVED
        if approve
        else ApprovalRequest.Status.REJECTED
    )
    approval.decided_by = user
    approval.decided_at = timezone.now()
    approval.rejection_reason = "" if approve else reason.strip()
    approval.save(
        update_fields=["status", "decided_by", "decided_at", "rejection_reason"]
    )

    return approval
