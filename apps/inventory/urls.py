from django.urls import path

from apps.inventory.views import (
    ProductDetailView,
    ProductListCreateView,
)

app_name = "inventory"

urlpatterns = [
    path(
        "products/",
        ProductListCreateView.as_view(),
        name="product-list-create",
    ),
    path(
        "products/<int:pk>/",
        ProductDetailView.as_view(),
        name="product-detail",
    ),
]