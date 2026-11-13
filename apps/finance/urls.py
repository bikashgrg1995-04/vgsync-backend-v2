from rest_framework.routers import SimpleRouter

from django.urls import path

from .cash_views import BalancesView, OpeningBalanceView, ReconciliationsView
from .views import ExpenseViewSet, IncomeViewSet

app_name = "finance"

router = SimpleRouter()
router.register("expenses", ExpenseViewSet, basename="expenses")
router.register("income", IncomeViewSet, basename="income")

urlpatterns = [
    path("balances/", BalancesView.as_view(), name="balances"),
    path("opening-balances/<str:method>/", OpeningBalanceView.as_view(), name="opening-balance"),
    path("reconciliations/", ReconciliationsView.as_view(), name="reconciliations"),
] + router.urls
