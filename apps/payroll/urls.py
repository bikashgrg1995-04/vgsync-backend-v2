from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import (
    BalanceDetailView,
    BalanceListView,
    PayrollEntryViewSet,
    PayrollPaymentViewSet,
    SalaryDetailView,
    SalaryListView,
)

app_name = "payroll"

router = SimpleRouter()
router.register("entries", PayrollEntryViewSet, basename="entries")
router.register("payments", PayrollPaymentViewSet, basename="payments")

urlpatterns = [
    path("salaries/", SalaryListView.as_view(), name="salaries"),
    path("salaries/<int:staff_id>/", SalaryDetailView.as_view(), name="salary"),
    path("balances/", BalanceListView.as_view(), name="balances"),
    path("balances/<int:staff_id>/", BalanceDetailView.as_view(), name="balance"),
] + router.urls
