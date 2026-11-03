from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),

    # Authentication and business context
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("api/v1/", include("apps.accounts.manage_urls")),

    # Business and inventory
    path("api/v1/businesses/", include("apps.businesses.urls")),
    path("api/v1/inventory/", include("apps.inventory.urls")),

    # Payroll
    path("api/v1/payroll/", include("apps.payroll.urls")),
    path("api/v1/finance/", include("apps.finance.urls")),
    path("api/v1/reports/", include("apps.reports.urls")),
    path("api/v1/purchases/", include("apps.purchases.urls")),
    path("api/v1/sales/", include("apps.sales.urls")),
    # Customers and suppliers: profiles and statements.
    path("api/v1/", include("apps.parties.urls")),
]

# Serve uploaded media files in development only.
# In production, serve MEDIA_ROOT from the web server or object storage.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
