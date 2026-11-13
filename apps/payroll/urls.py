from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import (
    BalanceDetailView,
    BalanceListView,
    MyPayView,
    MyStatementView,
    StatementView,
    PayrollEntryViewSet,
    PayrollPaymentViewSet,
    SalaryDetailView,
    SalaryListView,
    WorkSheetView, StaffAttendanceView,
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
    path(
        "balances/<int:staff_id>/statement/",
        StatementView.as_view(),
        name="statement",
    ),
    path("workdays/", WorkSheetView.as_view(), name="workdays"),
    path("workdays/staff/<int:staff_id>/", StaffAttendanceView.as_view(), name="staff-attendance"),
    path("me/", MyPayView.as_view(), name="my-pay"),
    path("me/statement/", MyStatementView.as_view(), name="my-statement"),
] + router.urls
