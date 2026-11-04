"""
Who is acting right now. The middleware opens a context for each request and
the API authentication fills in the user and business. Outside a request
(a shell, a migration, test set-up) nothing is logged.
"""

from contextvars import ContextVar

_state = ContextVar("audit_state", default=None)


def start(actor=None):
    return _state.set({"actor": actor, "business": None})


def stop(token):
    _state.reset(token)


def identify(actor, business):
    state = _state.get()

    if state is not None:
        state["actor"] = actor
        state["business"] = business


def current():
    return _state.get()
