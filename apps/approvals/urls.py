from rest_framework.routers import SimpleRouter

from .views import ApprovalRequestViewSet, ApprovalRuleViewSet

app_name = "approvals"

router = SimpleRouter()
router.register("rules", ApprovalRuleViewSet, basename="rules")
router.register("", ApprovalRequestViewSet, basename="requests")

urlpatterns = router.urls
