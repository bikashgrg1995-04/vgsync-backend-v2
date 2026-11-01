"""
Owner-only payroll APIs, scoped to the active business.
"""

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.scope import scoped_users
from apps.common.permissions import IsOwner

from .models import PayrollEntry, PayrollPayment, PayType, SalaryProfile
from .serializers import (
    BalanceDetailSerializer,
    BalanceSerializer,
    PayrollEntrySerializer,
    PayrollPaymentSerializer,
    SalaryProfileSerializer,
    VoidSerializer,
    staff_name,
)
from .services import (
    balance,
    create_payment,
    ensure_accruals,
    void_entry,
    void_payment,
)


def sync_accruals(profiles, today):
    """Record monthly salary that has fallen due since the last look."""
    for profile in profiles:
        if profile.pay_type == PayType.MONTHLY and profile.accrued_through < today:
            ensure_accruals(profile, today)


def load_balances(staff_qs, today):
    """
    ``[(staff, profile, balance)]`` for the given team members who have pay
    set up, using one query each for entries and payments.
    """
    profiles = list(
        SalaryProfile.objects.filter(user__in=staff_qs).select_related("user")
    )

    sync_accruals(profiles, today)

    staff_ids = [p.user_id for p in profiles]

    entries, payments = {}, {}

    for entry in PayrollEntry.objects.filter(
        staff_id__in=staff_ids, status=PayrollEntry.Status.ACTIVE
    ):
        entries.setdefault(entry.staff_id, []).append(entry)

    for payment in PayrollPayment.objects.filter(
        staff_id__in=staff_ids, status=PayrollPayment.Status.ACTIVE
    ):
        payments.setdefault(payment.staff_id, []).append(payment)

    return [
        (
            p.user,
            p,
            balance(p, entries.get(p.user_id, []), payments.get(p.user_id, []), today),
        )
        for p in profiles
    ]


def summary(staff, profile, bal):
    return {
        "staff": staff.pk,
        "staff_name": staff_name(staff),
        "is_active": staff.is_active,
        "pay_type": profile.pay_type,
        "total_earned": bal["total_earned"],
        "total_deducted": bal["total_deducted"],
        "total_paid": bal["total_paid"],
        "remaining": bal["remaining"],
        "previous_remaining": bal["previous_remaining"],
        "current_remaining": bal["current_remaining"],
        "advance": bal["advance"],
        "cycle_start": bal["cycle_start"],
        "next_salary_date": bal["next_salary_date"],
        "last_payment_date": bal["last_payment_date"],
    }


# ---------------------------------------------------------------------------
# Pay setup
# ---------------------------------------------------------------------------

class SalaryListView(generics.ListAPIView):
    """How each team member is paid."""

    permission_classes = [IsOwner]
    serializer_class = SalaryProfileSerializer
    filterset_fields = ("pay_type",)
    search_fields = ("user__username", "user__first_name", "user__last_name")
    ordering_fields = ("user__username", "rate", "updated_at")
    ordering = ("user__username",)

    def get_queryset(self):
        users = scoped_users(self.request.active_business)

        return SalaryProfile.objects.filter(user__in=users).select_related("user")


class SalaryDetailView(APIView):
    """``GET`` a team member's pay setup, or ``PUT`` to set or change it."""

    permission_classes = [IsOwner]

    def _staff(self, request, staff_id):
        return get_object_or_404(
            scoped_users(request.active_business),
            pk=staff_id,
        )

    def get(self, request, staff_id):
        staff = self._staff(request, staff_id)
        profile = get_object_or_404(
            SalaryProfile.objects.select_related("user"),
            user=staff,
        )

        return Response(SalaryProfileSerializer(profile).data)

    @transaction.atomic
    def put(self, request, staff_id):
        staff = self._staff(request, staff_id)
        today = timezone.localdate()

        profile = SalaryProfile.objects.filter(user=staff).first()

        if profile:
            # Salary that already fell due is recorded at the old rate first.
            profile = ensure_accruals(profile, today)

        serializer = SalaryProfileSerializer(profile, data=request.data)
        serializer.is_valid(raise_exception=True)

        extra = {"user": staff, "updated_by": request.user}

        if profile and serializer.validated_data["pay_type"] != profile.pay_type:
            # A new pay type starts today; the past is left as it was.
            extra["start_date"] = today
            extra["accrued_through"] = today

        saved = serializer.save(**extra)

        return Response(
            SalaryProfileSerializer(saved).data,
            status=status.HTTP_200_OK if profile else status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# What is owed
# ---------------------------------------------------------------------------

class BalanceListView(APIView):
    """
    Each person's account: what they earned, what was paid, what is still
    owed (``remaining``, with the part carried over from earlier periods in
    ``previous_remaining``) and any ``advance`` paid ahead.

    Filters: ``owing=true`` (still owed money), ``advance=true``,
    ``pay_type``, ``search``.
    """

    permission_classes = [IsOwner]

    def get(self, request):
        staff_qs = scoped_users(request.active_business)
        today = timezone.localdate()

        query = request.query_params

        if search := query.get("search"):
            staff_qs = staff_qs.filter(username__icontains=search) | staff_qs.filter(
                first_name__icontains=search
            ) | staff_qs.filter(last_name__icontains=search)

        if query.get("is_active") in ("true", "false"):
            staff_qs = staff_qs.filter(is_active=query["is_active"] == "true")

        rows = [
            summary(staff, profile, bal)
            for staff, profile, bal in load_balances(staff_qs, today)
        ]

        if pay_type := query.get("pay_type"):
            rows = [r for r in rows if r["pay_type"] == pay_type]

        if query.get("owing") == "true":
            rows = [r for r in rows if r["remaining"] > 0]

        if query.get("advance") == "true":
            rows = [r for r in rows if r["advance"] > 0]

        # Who is owed the most comes first.
        rows.sort(key=lambda r: (-r["remaining"], r["staff_name"].lower()))

        page = self.paginator.paginate_queryset(rows, request, view=self)

        return self.paginator.get_paginated_response(
            BalanceSerializer(page, many=True).data
        )

    @property
    def paginator(self):
        if not hasattr(self, "_paginator"):
            from apps.common.pagination import StandardPagination

            self._paginator = StandardPagination()

        return self._paginator


class BalanceDetailView(APIView):
    """One person's account, with each unsettled amount listed."""

    permission_classes = [IsOwner]

    def get(self, request, staff_id):
        staff_qs = scoped_users(request.active_business).filter(pk=staff_id)
        get_object_or_404(staff_qs)

        today = timezone.localdate()
        rows = load_balances(staff_qs, today)

        if not rows:
            return Response(
                {"detail": "Pay is not set up for this person.", "code": "no_salary"},
                status=status.HTTP_404_NOT_FOUND,
            )

        staff, profile, bal = rows[0]

        data = summary(staff, profile, bal)

        data["unsettled"] = [
            {
                "id": e.pk,
                "kind": e.kind,
                "entry_date": e.entry_date,
                "description": e.description,
                "amount": e.amount,
                "paid": e.paid,
                "remaining": e.remaining,
                "state": e.state,
                "carried_over": e.carried_over,
            }
            for e in bal["earnings"]
            if e.remaining > 0
        ]

        data["recent_payments"] = PayrollPayment.objects.filter(
            staff=staff, status=PayrollPayment.Status.ACTIVE
        ).select_related("staff")[:10]

        return Response(BalanceDetailSerializer(data).data)


# ---------------------------------------------------------------------------
# Entries and payments
# ---------------------------------------------------------------------------

class PayrollEntryViewSet(viewsets.ModelViewSet):
    """
    Daily wages, bonuses and deductions (monthly salary appears on its own
    when the salary day comes). Entries are voided, not deleted.
    """

    permission_classes = [IsOwner]
    serializer_class = PayrollEntrySerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    filterset_fields = {
        "kind": ["exact"],
        "status": ["exact"],
        "staff": ["exact"],
        "entry_date": ["gte", "lte"],
    }
    search_fields = ("staff__username", "staff__first_name", "staff__last_name")
    ordering_fields = ("entry_date", "amount", "created_at")

    def get_queryset(self):
        staff = scoped_users(self.request.active_business)

        # Bring monthly salary up to date so the list is complete.
        if self.action == "list":
            sync_accruals(
                SalaryProfile.objects.filter(user__in=staff).select_related("user"),
                timezone.localdate(),
            )

        return PayrollEntry.objects.filter(staff__in=staff).select_related("staff")

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        entry = self.get_object()

        serializer = VoidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        entry = void_entry(
            entry=entry,
            actor=request.user,
            reason=serializer.validated_data["reason"],
        )

        return Response(self.get_serializer(entry).data)


class PayrollPaymentViewSet(viewsets.ModelViewSet):
    """
    Payments to staff: full, part or advance. Each one also appears as a
    salary expense. A payment cannot be edited; void it and add a new one.
    """

    permission_classes = [IsOwner]
    serializer_class = PayrollPaymentSerializer
    http_method_names = ["get", "post", "head", "options"]

    filterset_fields = {
        "status": ["exact"],
        "staff": ["exact"],
        "method": ["exact"],
        "paid_on": ["gte", "lte"],
    }
    search_fields = ("staff__username", "staff__first_name", "staff__last_name")
    ordering_fields = ("paid_on", "amount", "created_at")

    def get_queryset(self):
        return PayrollPayment.objects.filter(
            staff__in=scoped_users(self.request.active_business)
        ).select_related("staff")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        payment = create_payment(
            staff=data["staff"],
            business=request.active_business,
            actor=request.user,
            amount=data["amount"],
            paid_on=data.get("paid_on", timezone.localdate()),
            method=data["method"],
            reference=data.get("reference", ""),
            notes=data.get("notes", ""),
        )

        return Response(
            self.get_serializer(payment).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        payment = self.get_object()

        serializer = VoidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        payment = void_payment(
            payment=payment,
            actor=request.user,
            reason=serializer.validated_data["reason"],
        )

        return Response(self.get_serializer(payment).data)
