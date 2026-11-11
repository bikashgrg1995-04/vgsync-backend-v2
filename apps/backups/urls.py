from rest_framework.routers import SimpleRouter

from .views import BackupViewSet

app_name = "backups"

router = SimpleRouter()
router.register("", BackupViewSet, basename="backups")

urlpatterns = router.urls
