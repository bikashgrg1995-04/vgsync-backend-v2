from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied


class PasswordChangeRequired(PermissionDenied):
    """
    The user must set a new password before using the API.

    The response body carries a machine-readable ``code`` so clients can
    send the user to the change-password screen instead of showing an error.
    """

    default_code = "password_change_required"

    def __init__(self):
        super().__init__(
            detail={
                "detail": "You must change your password before continuing.",
                "code": self.default_code,
            }
        )


class Conflict(APIException):
    """
    The request is valid but conflicts with the current state of the data
    (409), for example deleting a Position that still has staff assigned.

    The body is ``{"detail": "...", "code": "..."}``.
    """

    status_code = status.HTTP_409_CONFLICT
    default_code = "conflict"

    def __init__(self, detail, code=None):
        super().__init__(
            detail={
                "detail": detail,
                "code": code or self.default_code,
            }
        )
