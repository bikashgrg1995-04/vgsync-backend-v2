
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.common.permissions import HasActiveBusinessContext

from .models import Business
from .serializers import BranchListSerializer


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
