from . import context


class AuditContextMiddleware:
    """Opens an audit context for the length of one request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        actor = user if getattr(user, "is_authenticated", False) else None

        token = context.start(actor)

        try:
            return self.get_response(request)
        finally:
            context.stop(token)
