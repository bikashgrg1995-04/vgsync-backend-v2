from rest_framework.routers import SimpleRouter

from .views import (
    BikeUnitViewSet,
    CustomerViewSet,
    FollowupViewSet,
    SalePaymentViewSet,
    SaleViewSet,
)

app_name = "sales"

router = SimpleRouter()
# Registered before the empty prefix so "customers/" is not read as a sale id.
router.register("customers", CustomerViewSet, basename="customers")
router.register("payments", SalePaymentViewSet, basename="payments")
router.register("bikes", BikeUnitViewSet, basename="bikes")
router.register("followups", FollowupViewSet, basename="followups")
router.register("", SaleViewSet, basename="sales")

urlpatterns = router.urls
