from rest_framework.exceptions import PermissionDenied


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
