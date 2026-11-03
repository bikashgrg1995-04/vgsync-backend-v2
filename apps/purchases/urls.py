from rest_framework.routers import SimpleRouter

from .views import (
    CreditSettlementViewSet,
    PurchasePaymentViewSet,
    PurchaseReturnViewSet,
    PurchaseViewSet,
)

app_name = "purchases"

router = SimpleRouter()
# Registered before the empty prefix so "payments/" is not read as a purchase id.
# Suppliers have their own section: see ``apps.parties``.
router.register("payments", PurchasePaymentViewSet, basename="payments")
router.register("returns", PurchaseReturnViewSet, basename="returns")
router.register("credits", CreditSettlementViewSet, basename="credits")
router.register("", PurchaseViewSet, basename="purchases")

urlpatterns = router.urls
