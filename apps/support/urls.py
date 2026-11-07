from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import FaqViewSet, SupportContactView, SupportTicketViewSet

app_name = "support"

router = SimpleRouter()
router.register("faqs", FaqViewSet, basename="faqs")
router.register("tickets", SupportTicketViewSet, basename="tickets")

urlpatterns = [
    path("contact/", SupportContactView.as_view(), name="contact"),
    *router.urls,
]
