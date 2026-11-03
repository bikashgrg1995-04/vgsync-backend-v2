from django.urls import include, path
from rest_framework.routers import SimpleRouter

from .views import CustomerViewSet, SupplierViewSet

app_name = "parties"

customers = SimpleRouter()
customers.register("", CustomerViewSet, basename="customers")

suppliers = SimpleRouter()
suppliers.register("", SupplierViewSet, basename="suppliers")

urlpatterns = [
    path("customers/", include(customers.urls)),
    path("suppliers/", include(suppliers.urls)),
]
