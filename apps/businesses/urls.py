from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import BranchListView, BusinessViewSet

app_name = "businesses"

router = SimpleRouter()
router.register("", BusinessViewSet, basename="businesses")

# ``branches/`` must come before the router's ``{id}/`` route.
urlpatterns = [
    path("branches/", BranchListView.as_view(), name="branch-list"),
] + router.urls
