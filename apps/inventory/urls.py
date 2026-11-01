from django.urls import path

from apps.inventory.views import (
    ProductDetailView,
    ProductListCreateView,
    StockDetailView,
    StockListView,
    StockMovementDetailView,
    StockMovementListCreateView,
)

app_name = "inventory"

urlpatterns = [
    # Products
    path("products/", ProductListCreateView.as_view(), name="product-list-create"),
    path("products/<int:pk>/", ProductDetailView.as_view(), name="product-detail"),

    # Current business stock
    path("stock/", StockListView.as_view(), name="stock-list"),
    path("stock/<int:product_id>/", StockDetailView.as_view(), name="stock-detail"),

    # Stock movements
    path(
        "stock/movements/",
        StockMovementListCreateView.as_view(),
        name="stock-movement-list-create",
    ),
    path(
        "stock/movements/<int:pk>/",
        StockMovementDetailView.as_view(),
        name="stock-movement-detail",
    ),
]