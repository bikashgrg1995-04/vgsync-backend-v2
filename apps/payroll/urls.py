from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import PayrollRecordViewSet, SalaryDetailView, SalaryListView

app_name = "payroll"

router = SimpleRouter()
router.register("records", PayrollRecordViewSet, basename="records")

urlpatterns = [
    path("salaries/", SalaryListView.as_view(), name="salaries"),
    path("salaries/<int:staff_id>/", SalaryDetailView.as_view(), name="salary"),
] + router.urls
