from rest_framework.routers import SimpleRouter

from .views import NotificationViewSet

app_name = "notifications"

router = SimpleRouter()
router.register("", NotificationViewSet, basename="notifications")

urlpatterns = router.urls
