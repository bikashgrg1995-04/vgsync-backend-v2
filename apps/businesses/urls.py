
from django.urls import path

from .views import BranchListView

app_name = "businesses"

urlpatterns = [
    path("branches/", BranchListView.as_view(), name="branch-list"),
]
