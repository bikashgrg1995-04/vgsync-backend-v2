"""
One answer with the lists the web app needs right after sign-in.

Instead of ~15 separate requests, the app asks once. Each list is produced by the
very same view that serves it on its own address, called inside the server with the
caller's own sign-in, so permissions, the active business and the data shape are
exactly the same as for a normal request. A list the person may not see is simply
left out.

The packet is kept in the server cache for a short time per person and business,
so opening the app on a second device or tab straight away does not repeat the work.
"""

import json

from django.core.cache import cache
from django.http import HttpRequest
from django.test import RequestFactory
from django.urls import Resolver404, resolve
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

API_PREFIX = "/api/v1"
CACHE_SECONDS = 30

# Addresses (after /api/v1) the packet holds. Keep in step with the web app's
# first-load list (src/lib/warmup.ts); the app stores each one under the same address.
BOOTSTRAP_PATHS = (
    "/sales/?page=1&page_size=100",
    "/customers/?page=1&page_size=100",
    "/sales/bikes/?status=in_stock&page=1&page_size=100",
    "/sales/local-products/?page=1&page_size=100",
    "/inventory/stock/",
    "/purchases/?page=1&page_size=100",
    "/suppliers/?page=1&page_size=100",
    "/finance/expenses/?page=1&page_size=100",
    "/finance/expenses/summary/",
    "/staff/?page=1&page_size=100",
    "/positions/?page=1&page_size=100",
    "/inventory/transfers/?page_size=100&page=1",
    "/tasks/summary/",
    "/businesses/branches/",
)


def _call(path: str, authorization: str):
    """Run the normal view for one address; return its data, or None if not allowed."""
    url = API_PREFIX + path
    try:
        match = resolve(url.split("?")[0])
    except Resolver404:
        return None

    sub: HttpRequest = RequestFactory().get(url, HTTP_AUTHORIZATION=authorization)
    try:
        response = match.func(sub, *match.args, **match.kwargs)
        if hasattr(response, "render"):
            response.render()
    except Exception:  # one broken list must not spoil the others
        return None
    if response.status_code != 200:
        return None
    try:
        return json.loads(response.content)
    except ValueError:
        return None


class BootstrapView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        business = getattr(request, "active_business", None)
        key = f"bootstrap:{request.user.pk}:{getattr(business, 'pk', 0)}"

        packet = cache.get(key)
        if packet is None:
            auth = request.META.get("HTTP_AUTHORIZATION", "")
            items = {}
            for path in BOOTSTRAP_PATHS:
                data = _call(path, auth)
                if data is not None:
                    items[path] = data
            packet = {"generated_at": timezone.now().isoformat(), "items": items}
            cache.set(key, packet, CACHE_SECONDS)

        return Response(packet)
