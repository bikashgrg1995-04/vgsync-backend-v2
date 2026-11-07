from django.conf import settings
from rest_framework import mixins, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.common.permissions import HasActiveBusinessContext
from apps.common.throttling import SupportTicketRateThrottle

from .models import Faq, SupportTicket
from .serializers import FaqSerializer, SupportTicketSerializer


class FaqViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Published FAQs, in the order the team set. Anyone signed in may read them;
    they are not tied to a business.

    Search: ``?search=`` looks in the question and the answer.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = FaqSerializer
    search_fields = ("question", "answer")
    ordering_fields = ("sort_order", "id")
    ordering = ("sort_order", "id")

    def get_queryset(self):
        return Faq.objects.filter(is_published=True)


class SupportTicketViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """
    Support tickets of the active business, newest first. The owner, admin
    and accountant see every ticket of the business; staff see their own.

    Raising a ticket is allowed everywhere, including a company owner who is
    read-only inside a Branch: it asks for help and changes no business data.

    Filters: ``status``, ``category``. Search: ``?search=`` looks in the
    subject and the description.
    """

    permission_classes = [IsAuthenticated, HasActiveBusinessContext]
    serializer_class = SupportTicketSerializer
    filterset_fields = ("status", "category")
    search_fields = ("subject", "description")
    ordering_fields = ("id", "created_at", "status")
    ordering = ("-id",)

    def get_throttles(self):
        if self.action == "create":
            return [SupportTicketRateThrottle()]

        return super().get_throttles()

    def get_queryset(self):
        user = self.request.user
        queryset = SupportTicket.objects.filter(
            business=self.request.active_business
        ).select_related("created_by")

        if user.role == User.Role.STAFF:
            queryset = queryset.filter(created_by=user)

        return queryset

    def perform_create(self, serializer):
        serializer.save(
            business=self.request.active_business,
            created_by=self.request.user,
        )


class SupportContactView(APIView):
    """How to reach the VGSync team. The values come from settings."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            {
                "email": settings.SUPPORT_EMAIL,
                "phone": settings.SUPPORT_PHONE,
                "hours": settings.SUPPORT_HOURS,
                "reply_time": settings.SUPPORT_REPLY_TIME,
                "user_guide_url": settings.SUPPORT_USER_GUIDE_URL,
                "video_tutorials_url": settings.SUPPORT_VIDEO_TUTORIALS_URL,
            }
        )
