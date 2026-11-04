from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.exceptions import Conflict
from apps.common.permissions import IsOwner

from .serializers import CompanySerializer


class CompanyView(APIView):
    """
    The owner's own company: view it, or rename it. The code is fixed and a
    company is created, deactivated or removed only in Django Admin.
    """

    permission_classes = [IsOwner]

    def _company(self, request):
        company = request.active_business.company

        if company is None:
            raise Conflict(
                "An independent business does not belong to a company.",
                code="no_company",
            )

        return company

    def get(self, request):
        return Response(CompanySerializer(self._company(request)).data)

    def patch(self, request):
        company = self._company(request)

        serializer = CompanySerializer(company, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(serializer.data)
