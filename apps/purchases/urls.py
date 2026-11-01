from rest_framework.routers import SimpleRouter

from .views import PurchasePaymentViewSet, PurchaseViewSet, SupplierViewSet

app_name = "purchases"

router = SimpleRouter()
# Registered before the empty prefix so "suppliers/" is not read as a purchase id.
router.register("suppliers", SupplierViewSet, basename="suppliers")
router.register("payments", PurchasePaymentViewSet, basename="payments")
router.register("", PurchaseViewSet, basename="purchases")

urlpatterns = router.urls
