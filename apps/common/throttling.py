"""
Rate limits for the authentication endpoints.

Rates are configured in ``REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]``.

Throttle counters are stored in Django's cache. The default cache is
per-process, so with several worker processes each one counts separately.
For production configure a shared cache (for example Redis) in ``CACHES``.
"""

import hashlib
from collections.abc import Mapping

from rest_framework.throttling import SimpleRateThrottle


class LoginIPRateThrottle(SimpleRateThrottle):
    """Limits login attempts from one IP address."""

    scope = "login_ip"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class LoginAccountRateThrottle(SimpleRateThrottle):
    """
    Limits login attempts against one account (business code + username),
    whichever IP address they come from. This slows password guessing that
    is spread across many addresses.
    """

    scope = "login_account"

    def get_cache_key(self, request, view):
        data = request.data

        if not isinstance(data, Mapping):
            return None

        business_code = str(data.get("business_code", "")).strip().lower()
        username = str(data.get("username", "")).strip()

        # Missing fields are rejected by validation anyway.
        if not business_code or not username:
            return None

        # Hash so arbitrary user input never becomes part of a cache key.
        ident = hashlib.sha256(
            f"{business_code}\0{username}".encode()
        ).hexdigest()

        return self.cache_format % {"scope": self.scope, "ident": ident}


class TokenIPRateThrottle(SimpleRateThrottle):
    """Limits token refresh and logout requests from one IP address."""

    scope = "token_ip"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class ChangePasswordRateThrottle(SimpleRateThrottle):
    """
    Limits password-change attempts per user. Each attempt needs the
    current password, so this also limits guessing it from a stolen
    access token.
    """

    scope = "change_password"

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None

        return self.cache_format % {
            "scope": self.scope,
            "ident": request.user.pk,
        }


class ForgotPasswordIPRateThrottle(SimpleRateThrottle):
    """Limits password-reset requests from one IP address."""

    scope = "forgot_password_ip"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class ForgotPasswordAccountRateThrottle(SimpleRateThrottle):
    """
    Limits reset emails per account (business code + username) so one
    person cannot be flooded with emails from many addresses.
    """

    scope = "forgot_password_account"

    def get_cache_key(self, request, view):
        data = request.data

        if not isinstance(data, Mapping):
            return None

        business_code = str(data.get("business_code", "")).strip().lower()
        username = str(data.get("username", "")).strip()

        if not business_code or not username:
            return None

        ident = hashlib.sha256(
            f"{business_code}\0{username}".encode()
        ).hexdigest()

        return self.cache_format % {"scope": self.scope, "ident": ident}


class ResetPasswordRateThrottle(SimpleRateThrottle):
    """Limits reset-token guessing from one IP address."""

    scope = "reset_password"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }
