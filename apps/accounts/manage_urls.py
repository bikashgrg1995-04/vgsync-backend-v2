from rest_framework.routers import SimpleRouter

from .manage_views import PositionViewSet, StaffViewSet

app_name = "manage"

router = SimpleRouter()
router.register("staff", StaffViewSet, basename="staff")
router.register("positions", PositionViewSet, basename="positions")

urlpatterns = router.urls
