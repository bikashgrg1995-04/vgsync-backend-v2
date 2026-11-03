from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("profit-loss/", views.ProfitLossView.as_view(), name="profit-loss"),
    path("receivables/", views.ReceivablesView.as_view(), name="receivables"),
    path("payables/", views.PayablesView.as_view(), name="payables"),
    path("stock-value/", views.StockValueView.as_view(), name="stock-value"),
]
