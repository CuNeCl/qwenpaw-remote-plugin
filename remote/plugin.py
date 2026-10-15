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
                "通过 SSH 连接远端设备。连接后，本会话内的所有 shell 命令"
                "都会在远端机器上执行。"
            ),
            icon="🔗",
            enabled=False,
        )
        api.register_tool(
            tool_name="remote_reconnect",
            tool_func=remote_reconnect,
            description=(
                "使用缓存的连接参数重连远端机器。"
                "适用于之前的 SSH 连接已断开的情况。"
            ),
            icon="🔄",
            enabled=False,
        )
        api.register_tool(
            tool_name="remote_disconnect",
            tool_func=remote_disconnect,
            description="断开当前会话的远程 SSH 连接。",
            icon="🔌",
            enabled=False,
        )
        api.register_tool(
            tool_name="remote_list",
            tool_func=remote_list,
            description="查看当前会话的远程 SSH 连接状态。",
            icon="☁️",
            enabled=False,
        )
        api.register_tool(
            tool_name="remote_exec",
            tool_func=remote_exec,
            description=(
                "通过 SSH 在远端机器上显式执行一条命令。"
            ),
            icon="💻",
            enabled=False,
        )
        api.register_tool(
            tool_name="remote_info",
            tool_func=remote_info,
            description=(
                "查看远端机器的详细信息：系统、架构、内核、shell、"
                "CPU、内存、磁盘以及可用的开发工具。"
            ),
            icon="ℹ️",
            enabled=False,
        )
        api.register_tool(
            tool_name="remote_health",
            tool_func=remote_health,
            description=(
                "检查当前远程 SSH 连接的健康状态：连接状态、延迟、"
                "失败次数、是否可重连。"
            ),
            icon="❤️",
            enabled=False,
        )
        api.register_tool(
            tool_name="remote_set_cwd",
            tool_func=remote_set_cwd,
            description=(
                "设置当前会话的默认远程工作目录，"
                "之后所有命令都将在该目录下执行。"
            ),
            icon="📁",
            enabled=False,
        )
        api.register_tool(
            tool_name="remote_sudo",
            tool_func=remote_sudo,
            description=(
                "在远端机器上以 sudo 权限执行命令。"
                "需要预先配置 sudo 密码（仅 POSIX 远端支持）。"
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
