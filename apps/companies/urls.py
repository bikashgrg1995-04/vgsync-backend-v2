from django.urls import path

from .views import CompanyView

app_name = "companies"

urlpatterns = [
    path("", CompanyView.as_view(), name="company"),
]
