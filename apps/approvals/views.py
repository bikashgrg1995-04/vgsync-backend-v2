from django.db import transaction
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts import rbac
from apps.accounts.models import User
from apps.common.permissions import HasActiveBusinessContext, IsOwner

from .models import APPROVER_PERMISSION, ApprovalRequest, ApprovalRule
from .serializers import ApprovalRequestSerializer, ApprovalRuleSerializer
from .services import decide


class ApprovalRuleViewSet(viewsets.ModelViewSet):
    """Approval thresholds of the active business. Owner only."""

    permission_classes = [IsOwner]
    serializer_class = ApprovalRuleSerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    filterset_fields = ("kind", "is_active")

    def get_queryset(self):
        return ApprovalRule.objects.filter(
            business=self.request.active_business
        )

    def perform_create(self, serializer):
        serializer.save(
            business=self.request.active_business,
            updated_by=self.request.user,
        )

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # One rule per kind: posting again updates it.
        existing = self.get_queryset().filter(
            kind=serializer.validated_data["kind"]
        ).first()

        if existing:
            serializer = self.get_serializer(
                existing, data=request.data, partial=True
            )
            serializer.is_valid(raise_exception=True)
            self.perform_update(serializer)

            return Response(serializer.data)

        self.perform_create(serializer)

        return Response(serializer.data, status=201)


class ApprovalRequestViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """
    Held operations. The owner sees all. Others see their own requests and
    the kinds they may approve. Decide with ``approve`` / ``reject``.
    """

    permission_classes = [IsAuthenticated, HasActiveBusinessContext]
    serializer_class = ApprovalRequestSerializer
    filterset_fields = ("status", "kind")
    ordering = ("-id",)

    def get_queryset(self):
        user = self.request.user
        queryset = ApprovalRequest.objects.filter(
            business=self.request.active_business
        ).select_related("created_by", "decided_by")

        if user.role == User.Role.OWNER:
            return queryset

        mine = {
            kind
            for kind, code in APPROVER_PERMISSION.items()
            if rbac.has_permission(user, code)
        }

        from django.db.models import Q

        return queryset.filter(Q(created_by=user) | Q(kind__in=mine))

    def _decide(self, request, approve):
        with transaction.atomic():
            approval = self.get_object()
            approval = ApprovalRequest.objects.select_for_update().get(
                pk=approval.pk
            )
            decide(
                approval,
                request.user,
                approve,
                access_mode=request.access_mode,
                reason=request.data.get("reason", "") or "",
            )

        return Response(self.get_serializer(approval).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        return self._decide(request, True)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self._decide(request, False)
