
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.common.exceptions import Conflict
from apps.common.permissions import HasActiveBusinessContext, IsOwner

from .models import Business
from .serializers import BranchListSerializer, BusinessSerializer


class BranchListView(APIView):
    permission_classes = [
        IsAuthenticated,
        HasActiveBusinessContext,
    ]

    def get(self, request):
        user = request.user
        active_business = request.active_business

        # Only company-wide roles can switch between company businesses.
        if (
            not user.company_id
            or user.role not in [
                User.Role.OWNER,
                User.Role.ADMIN,
                User.Role.ACCOUNTANT,
            ]
        ):
            return Response(
                {
                    "detail": (
                        "You are not allowed to view company branches."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # The active context must belong to the user's company.
        if active_business.company_id != user.company_id:
            return Response(
                {"detail": "Invalid company business context."},
                status=status.HTTP_403_FORBIDDEN,
            )

        branches = (
            Business.objects
            .filter(
                company_id=user.company_id,
                business_type=Business.BusinessType.BRANCH,
                is_active=True,
                parent_business__is_active=True,
            )
            .order_by("name")
        )

        serializer = BranchListSerializer(branches, many=True)

        return Response(serializer.data, status=status.HTTP_200_OK)


class BusinessViewSet(viewsets.ModelViewSet):
    """
    The businesses of the owner's tenant.

    A company owner sees the Head Office and every Branch, and can add a
    Branch (from the Head Office), edit businesses, and deactivate or
    reactivate a Branch. An independent owner sees only their own business
    and can edit it. A business is never deleted, and its code never
    changes. Writes need full access, so a company owner makes them from
    the Head Office, not from inside a Branch.
    """

    permission_classes = [IsOwner]
    serializer_class = BusinessSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    filterset_fields = ("business_type", "is_active")
    search_fields = ("name", "business_code")
    ordering_fields = ("name", "created_at")
    ordering = ("business_type", "name")

    def get_queryset(self):
        business = self.request.active_business

        if business.company_id:
            return Business.objects.filter(
                company_id=business.company_id
            ).select_related("parent_business")

        return Business.objects.filter(pk=business.pk)

    def create(self, request, *args, **kwargs):
        head_office = request.active_business

        if head_office.business_type != Business.BusinessType.HEAD_OFFICE:
            raise Conflict(
                "Branches are added by a company from its Head Office.",
                code="not_head_office",
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        branch = serializer.save(
            business_type=Business.BusinessType.BRANCH,
            company=head_office.company,
            parent_business=head_office,
            created_by=request.user,
        )

        return Response(
            self.get_serializer(branch).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        branch = self.get_object()

        if branch.business_type != Business.BusinessType.BRANCH:
            raise Conflict(
                "Only a branch can be deactivated here.", code="not_a_branch"
            )

        if branch.pk == request.active_business.pk:
            raise Conflict(
                "Switch to the Head Office to deactivate this branch.",
                code="active_business",
            )

        if not branch.is_active:
            raise Conflict("This branch is already inactive.", code="invalid_status")

        branch.is_active = False
        branch.save(update_fields=["is_active"])

        return Response(self.get_serializer(branch).data)

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        branch = self.get_object()

        if branch.business_type != Business.BusinessType.BRANCH:
            raise Conflict(
                "Only a branch can be activated here.", code="not_a_branch"
            )

        if branch.is_active:
            raise Conflict("This branch is already active.", code="invalid_status")

        branch.is_active = True
        branch.save(update_fields=["is_active"])

        return Response(self.get_serializer(branch).data)
