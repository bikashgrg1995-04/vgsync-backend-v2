from django.urls import path

from apps.inventory.views import (
    ProductDetailView,
    ProductListCreateView,
    StockDetailView,
    StockListView,
    StockMovementDetailView,
    StockMovementListCreateView,
)

from apps.inventory.transfer_views import (
    TransferCancelView,
    TransferDetailView,
    TransferDispatchView,
    TransferListCreateView,
    TransferReceiveView,
    TransferReturnReceiveView,
    TransferReturnReviewView,
    TransferReturnView,
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

    # Stock orders (Branch -> Head Office)
    path("transfers/", TransferListCreateView.as_view(), name="transfer-list-create"),
    path("transfers/<int:pk>/", TransferDetailView.as_view(), name="transfer-detail"),
    path("transfers/<int:pk>/cancel/", TransferCancelView.as_view(), name="transfer-cancel"),
    path("transfers/<int:pk>/dispatch/", TransferDispatchView.as_view(), name="transfer-dispatch"),
    path("transfers/<int:pk>/receive/", TransferReceiveView.as_view(), name="transfer-receive"),
    path("transfers/<int:pk>/return/", TransferReturnView.as_view(), name="transfer-return"),
    path(
        "transfers/<int:pk>/returns/<int:batch_id>/receive/",
        TransferReturnReceiveView.as_view(),
        name="transfer-return-receive",
    ),
    path(
        "transfers/<int:pk>/returns/<int:batch_id>/review/",
        TransferReturnReviewView.as_view(),
        name="transfer-return-review",
    ),
]
