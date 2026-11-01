"""
Owner-only payroll APIs, scoped to the active business.
"""

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils import timezone

from apps.accounts.scope import scoped_users
from apps.common.exceptions import Conflict
from apps.common.permissions import IsOwner

from .models import PayrollRecord, SalaryProfile
from .serializers import (
    GenerateSerializer,
    PaySerializer,
    PayrollRecordSerializer,
    SalaryProfileSerializer,
    VoidSerializer,
)
from .services import generate_drafts, mark_paid, void_record


class SalaryListView(generics.ListAPIView):
    """Salaries of the active business's team."""

    permission_classes = [IsOwner]
    serializer_class = SalaryProfileSerializer
    search_fields = ("user__username", "user__first_name", "user__last_name")
    ordering_fields = ("user__username", "basic_salary", "updated_at")
    ordering = ("user__username",)

    def get_queryset(self):
        users = scoped_users(self.request.active_business)

        return SalaryProfile.objects.filter(user__in=users).select_related("user")


class SalaryDetailView(APIView):
    """``GET`` a team member's salary, or ``PUT`` to set or change it."""

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

    def put(self, request, staff_id):
        staff = self._staff(request, staff_id)

        profile = SalaryProfile.objects.filter(user=staff).first()
        serializer = SalaryProfileSerializer(profile, data=request.data)
        serializer.is_valid(raise_exception=True)

        saved = serializer.save(user=staff, updated_by=request.user)

        return Response(
            SalaryProfileSerializer(saved).data,
            status=status.HTTP_200_OK if profile else status.HTTP_201_CREATED,
        )


class PayrollRecordViewSet(viewsets.ModelViewSet):
    """
    Payroll records. ``DELETE`` removes a draft only; a paid record is voided
    with a reason so payment history stays intact.
    """

    permission_classes = [IsOwner]
    serializer_class = PayrollRecordSerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    filterset_fields = {
        "status": ["exact"],
        "staff": ["exact"],
        "period_start": ["gte", "lte"],
        "period_end": ["gte", "lte"],
        "paid_on": ["gte", "lte"],
    }
    search_fields = ("staff__username", "staff__first_name", "staff__last_name")
    ordering_fields = ("period_start", "net_salary", "paid_on", "created_at")

    def get_queryset(self):
        return PayrollRecord.objects.filter(
            business=self.request.active_business
        ).select_related("staff")

    def destroy(self, request, *args, **kwargs):
        record = self.get_object()

        if record.status != PayrollRecord.Status.DRAFT:
            raise Conflict(
                "Only a draft can be deleted. Void a paid record instead.",
                code="document_not_editable",
            )

        record.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        record = self.get_object()

        serializer = PaySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        record = mark_paid(
            record=record,
            actor=request.user,
            paid_on=data.get("paid_on", timezone.localdate()),
            payment_method=data["payment_method"],
            payment_reference=data.get("payment_reference", ""),
        )

        return Response(self.get_serializer(record).data)

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        record = self.get_object()

        serializer = VoidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        record = void_record(
            record=record,
            actor=request.user,
            reason=serializer.validated_data["reason"],
        )

        return Response(self.get_serializer(record).data)

    @action(detail=False, methods=["post"])
    def generate(self, request):
        """Create a draft for every active team member with a salary."""
        serializer = GenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        business = request.active_business

        with transaction.atomic():
            created, skipped = generate_drafts(
                business=business,
                staff_queryset=scoped_users(business),
                period_start=serializer.validated_data["period_start"],
                period_end=serializer.validated_data["period_end"],
                actor=request.user,
            )

        return Response(
            {
                "created": self.get_serializer(created, many=True).data,
                "skipped": skipped,
            },
            status=status.HTTP_201_CREATED,
        )
