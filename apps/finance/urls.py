from rest_framework.routers import SimpleRouter

from .views import ExpenseViewSet, IncomeViewSet

app_name = "finance"

router = SimpleRouter()
router.register("expenses", ExpenseViewSet, basename="expenses")
router.register("income", IncomeViewSet, basename="income")

urlpatterns = router.urls
