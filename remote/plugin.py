"""Remote SSH Plugin for QwenPaw.

Enables SSH connections to remote devices. When a connection is active for a
session, all shell commands execute transparently on the remote machine.

Targets the QwenPaw 2.0+ plugin API: ``register_middleware`` for the SSH
interception, ``register_control_command`` for ``/remote``, and
``register_http_router`` for the management REST API.
"""

import logging

logger = logging.getLogger(__name__)


class RemotePlugin:
    """Remote SSH plugin entry point."""

    def __init__(self):
        self._api = None

    def register(self, api):
        """Register tools, command, middleware, router and hooks."""
        self._api = api

        from .tools.remote_connect import remote_connect, remote_reconnect
        from .tools.remote_disconnect import remote_disconnect
        from .tools.remote_list import remote_list
        from .tools.remote_exec import remote_exec
        from .tools.remote_info import remote_info
        from .tools.remote_health import remote_health
        from .tools.remote_set_cwd import remote_set_cwd
        from .tools.remote_sudo import remote_sudo

        api.register_tool(
            tool_name="remote_connect",
            tool_func=remote_connect,
            description=(
                "Connect to a remote device via SSH. After connecting, "
                "all shell commands in this conversation will execute "
                "on the remote machine."
            ),
            icon="🔗",
            enabled=False,
        )
        api.register_tool(
            tool_name="remote_reconnect",
            tool_func=remote_reconnect,
            description=(
                "Reconnect to the remote machine using cached connection "
                "parameters. Use when the previous SSH connection was lost."
            ),
            icon="🔄",
            enabled=False,
        )
        api.register_tool(
            tool_name="remote_disconnect",
            tool_func=remote_disconnect,
            description="Disconnect from the current remote SSH session.",
            icon="🔌",
            enabled=False,
        )
        api.register_tool(
            tool_name="remote_list",
            tool_func=remote_list,
            description="Show the current remote SSH connection status.",
            icon="☁️",
            enabled=False,
        )
        api.register_tool(
            tool_name="remote_exec",
            tool_func=remote_exec,
            description=(
                "Explicitly execute a command on the remote machine via SSH."
            ),
            icon="💻",
            enabled=False,
        )
        api.register_tool(
            tool_name="remote_info",
            tool_func=remote_info,
            description=(
                "Show detailed information about the remote machine: "
                "OS, architecture, kernel, shell, CPU, memory, disk, "
                "and available development tools."
            ),
            icon="ℹ️",
            enabled=False,
        )
        api.register_tool(
            tool_name="remote_health",
            tool_func=remote_health,
            description=(
                "Check the health status of the current remote SSH "
                "connection: status, latency, failures, reconnect availability."
            ),
            icon="❤️",
            enabled=False,
        )
        api.register_tool(
            tool_name="remote_set_cwd",
            tool_func=remote_set_cwd,
            description=(
                "Set the default remote working directory for this session. "
                "All subsequent commands will execute in this directory."
            ),
            icon="📁",
            enabled=False,
        )
        api.register_tool(
            tool_name="remote_sudo",
            tool_func=remote_sudo,
            description=(
                "Execute a command with sudo privileges on the remote machine. "
                "Requires a configured sudo password (POSIX remotes only)."
            ),
            icon="🛡️",
            enabled=False,
        )

        self._register_control_command(api)
        self._register_middleware(api)
        _mount_router(api)

        api.register_shutdown_hook(
            hook_name="remote_cleanup",
            callback=self._on_shutdown,
            priority=50,
        )
        logger.info("[Remote] Plugin registered")

    def _register_control_command(self, api):
        """Register /remote, tolerating host control-command API drift."""
        try:
            from .tools.remote_command import RemoteCommandHandler
        except ImportError as exc:
            logger.error(
                "[Remote] /remote command unavailable: the host control "
                "command API could not be imported (%s). Tools and the SSH "
                "middleware remain functional.",
                exc,
            )
            return

        try:
            api.register_control_command(
                handler=RemoteCommandHandler(),
                priority_level=10,
            )
            logger.info("[Remote] Registered /remote control command")
        except Exception as exc:
            logger.error("[Remote] Failed to register /remote command: %s", exc)

    def _register_middleware(self, api):
        """Register the SSH middleware factory (QwenPaw 2.0+ API)."""
        from .shell_wrapper import make_ssh_middleware_factory

        factory = make_ssh_middleware_factory()
        if factory is None:
            logger.error(
                "[Remote] SSH middleware could not be created; shell commands "
                "will NOT be forwarded to the remote machine"
            )
            return

        api.register_middleware(
            middleware_factory=factory,
            priority=50,
        )
        logger.info("[Remote] Registered SSH middleware")

    async def _on_shutdown(self):
        """Cleanup on application shutdown."""
        logger.info("[Remote] Plugin shutting down...")
        try:
            from .ssh_manager import get_ssh_manager

            await get_ssh_manager().close_all()
            logger.info("[Remote] All SSH connections closed")
        except Exception as e:
            logger.warning("[Remote] Failed to close SSH connections: %s", e)


def _mount_router(api):
    """Mount the management REST API at ``/api/remote``."""
    try:
        from .routers.connections import router

        api.register_http_router(
            router=router,
            prefix="/remote",
            tags=["remote"],
        )
        logger.info("[Remote] HTTP router mounted at /api/remote")
    except Exception as e:
        logger.error("[Remote] Failed to mount HTTP router: %s", e)


plugin = RemotePlugin()
