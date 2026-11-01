from rest_framework.routers import SimpleRouter

from .views import ExpenseViewSet

app_name = "finance"

router = SimpleRouter()
router.register("expenses", ExpenseViewSet, basename="expenses")

urlpatterns = router.urls
