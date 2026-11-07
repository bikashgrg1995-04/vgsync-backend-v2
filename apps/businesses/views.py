
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
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


class BusinessViewSet(viewsets.ReadOnlyModelViewSet):
    """
    The businesses of the owner's tenant, for reading only.

    A company owner sees the Head Office and every Branch. An independent
    owner sees only their own business. Businesses are created, edited,
    switched off (for example when a subscription is not paid) and switched
    on again only by a super admin in Django Admin. Nobody changes them here.
    """

    permission_classes = [IsOwner]
    serializer_class = BusinessSerializer

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
