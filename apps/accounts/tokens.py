from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)


def revoke_all_refresh_tokens(user):
    """
    Blacklist every refresh token issued to ``user`` so all of their
    sessions must log in again. Already-blacklisted tokens are skipped.

    Access tokens already issued stay valid until they expire (15 minutes
    by default).
    """
    outstanding = OutstandingToken.objects.filter(
        user=user,
        blacklistedtoken__isnull=True,
    )

    BlacklistedToken.objects.bulk_create(
        [BlacklistedToken(token=token) for token in outstanding],
        ignore_conflicts=True,
    )
