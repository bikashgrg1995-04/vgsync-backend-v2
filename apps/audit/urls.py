from rest_framework.routers import SimpleRouter

from .views import AuditLogViewSet

app_name = "audit"

router = SimpleRouter()
router.register("", AuditLogViewSet, basename="audit")

urlpatterns = router.urls
