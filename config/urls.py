
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),

    path("api/v1/auth/", include("apps.accounts.urls")),
    path("api/v1/businesses/", include("apps.businesses.urls")),
    path("api/v1/inventory/", include("apps.inventory.urls")),
]

# Serve uploaded media files in development only.
# In production, serve MEDIA_ROOT from the web server or object storage.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
