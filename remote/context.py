"""Context variables for remote SSH session scoping.

``remote_session_id`` is set for the duration of a chat request by the SSH
middleware, so the ``remote_*`` tools resolve the same session (and therefore
the same SSH connection) the middleware uses. ``remote_agent_id`` carries the
agent that owns the session; it is recorded against connections so the
management UI can find them without knowing a session id (the settings route
has no current session).
"""

from contextvars import ContextVar

remote_session_id: ContextVar[str | None] = ContextVar(
    "remote_session_id",
    default=None,
)
remote_agent_id: ContextVar[str | None] = ContextVar(
    "remote_agent_id",
    default=None,
)


def get_remote_session_id() -> str | None:
    """Get the current remote session ID from context."""
    return remote_session_id.get()


def get_remote_agent_id() -> str | None:
    """Get the agent id that owns the current session from context."""
    return remote_agent_id.get()
