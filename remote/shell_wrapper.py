"""SSH-aware shell execution middleware (QwenPaw 2.0+).

Registers an AgentScope ``MiddlewareBase`` factory through
``PluginApi.register_middleware``. The middleware intercepts
``execute_shell_command`` and runs it on the session's remote host instead
of the local machine.
"""

import json
import logging
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from .ssh_manager import get_ssh_manager

if TYPE_CHECKING:
    from agentscope.agent import Agent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool call parsing
# ---------------------------------------------------------------------------

def _parse_tool_call(tool_call: Any) -> tuple[str, dict[str, Any]]:
    """Return ``(tool_name, tool_input)`` from a middleware tool_call.

    AgentScope hands the tool call over as a block object; dict-shaped
    payloads are accepted as well. ``input`` may be a JSON string.
    """
    if tool_call is None:
        return "", {}

    if isinstance(tool_call, dict):
        tool_name = tool_call.get("name") or ""
        raw_input = tool_call.get("input")
    else:
        tool_name = getattr(tool_call, "name", "") or ""
        raw_input = getattr(tool_call, "input", None)

    if isinstance(raw_input, str):
        try:
            tool_input = json.loads(raw_input)
        except (json.JSONDecodeError, TypeError):
            tool_input = {}
    elif isinstance(raw_input, dict):
        tool_input = raw_input
    else:
        tool_input = {}

    return str(tool_name), tool_input


def _coerce_timeout(value: Any, default: float = 60.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Remote execution
# ---------------------------------------------------------------------------

async def _execute_remote(
    session_id: str,
    command: str,
    timeout: float = 60.0,
    cwd: str = "",
    sudo: bool = False,
) -> ToolResponse:
    """Execute a command on the remote host via SSHManager.

    Output format mirrors the local ``execute_shell_command`` result, with a
    ``[remote: user@host]`` prefix added.
    """
    manager = get_ssh_manager()
    conn = manager.get_connection(session_id)
    if conn is None:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text="Error: No active SSH connection for this session. "
                    "Use remote_connect first.",
                ),
            ],
        )

    try:
        returncode, stdout_str, stderr_str = await manager.execute_command(
            session_id,
            command,
            timeout,
            cwd or None,
            sudo=sudo,
        )
    except Exception as e:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: Remote command execution failed: {e}",
                ),
            ],
        )

    prefix = f"[remote: {conn.username}@{conn.host}]"
    if sudo:
        prefix = f"{prefix} [sudo]"

    if returncode == 0:
        if stdout_str:
            response_text = f"{prefix}\n{stdout_str}"
        else:
            response_text = f"{prefix}\nCommand executed successfully (no output)."
        if stderr_str:
            response_text += f"\n[stderr]\n{stderr_str}"
    else:
        response_parts = [
            f"{prefix}\nCommand failed with exit code {returncode}.",
        ]
        if stdout_str:
            response_parts.append(f"\n[stdout]\n{stdout_str}")
        if stderr_str:
            response_parts.append(f"\n[stderr]\n{stderr_str}")
        response_text = "".join(response_parts)

    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=response_text,
            ),
        ],
    )


def _connection_lost_response(session_id: str) -> ToolResponse:
    """Refuse local execution when a session's remote link was lost.

    Falling back to the local machine would silently run destructive
    commands on the wrong host.
    """
    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=(
                    "[remote] Connection lost — the command was NOT executed.\n"
                    "This session had an SSH connection that is no longer "
                    "alive. Reconnect with remote_reconnect (or "
                    "`/remote reconnect`) and retry. Local execution is "
                    "deliberately blocked so commands cannot run on the "
                    "wrong machine."
                ),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Middleware factory (PluginApi.register_middleware)
# ---------------------------------------------------------------------------

def make_ssh_middleware_factory():
    """Create a middleware factory for ``api.register_middleware()``.

    The factory is called once per request during agent assembly:
    ``factory(ctx, agent_config) -> MiddlewareBase | None``.
    """
    try:
        from agentscope.middleware import MiddlewareBase
    except ImportError:
        logger.error(
            "[Remote] agentscope.middleware is unavailable; SSH middleware "
            "not registered"
        )
        return None

    class SSHMiddleware(MiddlewareBase):
        """Redirects ``execute_shell_command`` to the session's SSH host."""

        def __init__(self, session_id: str, agent_id: str = "") -> None:
            self._session_id = session_id
            self._agent_id = agent_id

        async def on_acting(
            self,
            agent: "Agent",
            input_kwargs: dict[str, Any],
            next_handler: Callable[..., AsyncGenerator[Any, None]],
        ) -> AsyncGenerator[Any, None]:
            # Expose the session (and its owning agent) to the remote_* tools
            # via ContextVars so they resolve the same SSH connection this
            # middleware uses.
            from .context import remote_agent_id, remote_session_id

            manager = get_ssh_manager()
            manager.remember_session_owner(self._session_id, self._agent_id)
            # A brand-new chat has no session id until its first turn, so a
            # connect requested from the settings page was deferred to here.
            await manager.materialize_pending(self._session_id, self._agent_id)

            session_token = remote_session_id.set(self._session_id)
            agent_token = remote_agent_id.set(self._agent_id or None)
            try:
                async for chunk in self._handle_acting(
                    input_kwargs,
                    next_handler,
                ):
                    yield chunk
            finally:
                # Never leak one request's session into another task.
                remote_session_id.reset(session_token)
                remote_agent_id.reset(agent_token)

        async def _handle_acting(
            self,
            input_kwargs: dict[str, Any],
            next_handler: Callable[..., AsyncGenerator[Any, None]],
        ) -> AsyncGenerator[Any, None]:
            tool_name, tool_input = _parse_tool_call(input_kwargs.get("tool_call"))

            if tool_name != "execute_shell_command":
                async for chunk in next_handler(**input_kwargs):
                    yield chunk
                return

            manager = get_ssh_manager()
            conn = manager.get_connection(self._session_id)
            if conn is None:
                if manager.can_reconnect(self._session_id):
                    # The session had a connection that died. Running the
                    # command locally would target the wrong machine.
                    yield _connection_lost_response(self._session_id)
                    return
                # Never connected in this session — local execution is what
                # the caller expects.
                async for chunk in next_handler(**input_kwargs):
                    yield chunk
                return

            command = tool_input.get("command", "")
            timeout = _coerce_timeout(tool_input.get("timeout", 60.0))
            cwd = tool_input.get("cwd", "")

            # cwd is handled by SSHManager, which wraps it per platform.
            yield await _execute_remote(self._session_id, command, timeout, cwd)

    def factory(ctx: Any, agent_config: Any):
        session_id = str(getattr(ctx, "session_id", "") or "").strip()
        agent_id = str(getattr(ctx, "agent_id", "") or "").strip()
        if not session_id:
            # Returning None skips this middleware for the request.
            return None
        return SSHMiddleware(session_id, agent_id)

    return factory
