from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("profit-loss/", views.ProfitLossView.as_view(), name="profit-loss"),
    path("receivables/", views.ReceivablesView.as_view(), name="receivables"),
    path("payables/", views.PayablesView.as_view(), name="payables"),
    path("stock-value/", views.StockValueView.as_view(), name="stock-value"),
    path("sales-trend/", views.SalesTrendView.as_view(), name="sales-trend"),
    path("top-products/", views.TopProductsView.as_view(), name="top-products"),
    path("top-customers/", views.TopCustomersView.as_view(), name="top-customers"),
    path("top-suppliers/", views.TopSuppliersView.as_view(), name="top-suppliers"),
    path("payroll-summary/", views.PayrollSummaryView.as_view(), name="payroll-summary"),
]
