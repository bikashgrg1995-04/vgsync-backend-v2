from rest_framework.routers import SimpleRouter

from .views import TaskViewSet

app_name = "tasks"

router = SimpleRouter()
router.register("", TaskViewSet, basename="tasks")

urlpatterns = router.urls
