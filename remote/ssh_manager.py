"""SSH connection manager singleton using paramiko."""

import asyncio
import logging
import os
import select
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import platform as remote_platform
from .store import KNOWN_HOSTS_FILE, find_jump_host_by_name, get_jump_host
from .ssh_types import (
    RemoteEnvSnapshot,
    SSHConnectionInfo,
    SSHHealthInfo,
    SudoState,
    normalize_host_and_port,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level SSH helpers
# ---------------------------------------------------------------------------

def _read_channel(channel, timeout: float) -> tuple[str, str, bool]:
    """Drain an SSH channel until it closes.

    Returns ``(stdout, stderr, timed_out)``. On timeout the channel is closed
    and whatever was received so far is returned.
    """
    stdout = b""
    stderr = b""
    start = time.monotonic()

    while not channel.exit_status_ready():
        if time.monotonic() - start > timeout:
            channel.close()
            return (
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
                True,
            )
        readable, _, _ = select.select([channel], [], [], min(1.0, timeout))
        if readable:
            if channel.recv_ready():
                stdout += channel.recv(65536)
            if channel.recv_stderr_ready():
                stderr += channel.recv_stderr(65536)

    while channel.recv_ready():
        stdout += channel.recv(65536)
    while channel.recv_stderr_ready():
        stderr += channel.recv_stderr(65536)

    return (
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
        False,
    )


def _run_on_client(
    client: Any,
    command: str,
    timeout: float = 20.0,
    stdin_data: str = "",
) -> tuple[int, str, str, bool]:
    """Run a command on an already-connected SSHClient.

    Returns ``(returncode, stdout, stderr, timed_out)``.
    """
    transport = client.get_transport()
    if transport is None or not transport.is_active():
        raise ConnectionError("SSH transport is closed")

    channel = transport.open_session()
    channel.settimeout(timeout)
    channel.exec_command(command)

    if stdin_data:
        channel.sendall(stdin_data.encode("utf-8"))
    channel.shutdown_write()

    stdout, stderr, timed_out = _read_channel(channel, timeout)
    returncode = -1 if timed_out else channel.recv_exit_status()
    channel.close()
    return returncode, stdout, stderr, timed_out


def _build_connect_kwargs(
    *,
    hostname: str,
    port: int,
    username: str,
    password: str = "",
    key_path: str = "",
    passphrase: str = "",
    sock: object | None = None,
) -> dict[str, Any]:
    connect_kwargs: dict[str, Any] = {
        "hostname": hostname,
        "port": port,
        "username": username,
        "timeout": 15,
    }
    if password:
        connect_kwargs["password"] = password
    if key_path:
        connect_kwargs["key_filename"] = key_path
    if passphrase:
        connect_kwargs["passphrase"] = passphrase
    if sock is not None:
        connect_kwargs["sock"] = sock
    return connect_kwargs


def _load_known_hosts(client: Any) -> None:
    """Load the system, per-user and plugin known_hosts files."""
    try:
        client.load_system_host_keys()
    except Exception as exc:  # pragma: no cover - platform dependent
        logger.debug("[Remote] Could not load system host keys: %s", exc)

    candidates = [Path.home() / ".ssh" / "known_hosts", KNOWN_HOSTS_FILE]
    for known_hosts in candidates:
        try:
            if known_hosts.is_file():
                client.load_host_keys(str(known_hosts))
        except Exception as exc:  # pragma: no cover - platform dependent
            logger.debug("[Remote] Could not load %s: %s", known_hosts, exc)


def _remember_host_key(host: str, port: int, key: Any) -> None:
    """Record an explicitly trusted host key in the plugin known_hosts file.

    Trust-on-first-use: the key is written once, so later connections are
    verified against it instead of needing ``accept_new_host_key`` forever.
    """
    if key is None:
        return

    entry_host = host if port == 22 else f"[{host}]:{port}"
    try:
        line = f"{entry_host} {key.get_name()} {key.get_base64()}\n"
    except Exception as exc:  # pragma: no cover - unexpected key object
        logger.debug("[Remote] Could not serialise host key: %s", exc)
        return

    try:
        KNOWN_HOSTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        existing = (
            KNOWN_HOSTS_FILE.read_text(encoding="utf-8")
            if KNOWN_HOSTS_FILE.is_file()
            else ""
        )
        if line in existing:
            return
        if not existing:
            existing = (
                "# Host keys trusted through the Remote SSH plugin "
                "(accept_new_host_key).\n"
            )
        KNOWN_HOSTS_FILE.write_text(existing + line, encoding="utf-8")
        try:
            os.chmod(KNOWN_HOSTS_FILE, 0o600)
        except OSError:  # pragma: no cover - Windows / non-POSIX filesystems
            pass
        logger.info("[Remote] Recorded host key for %s:%d", host, port)
    except OSError as exc:
        logger.warning("[Remote] Could not record host key: %s", exc)


def _remember_client_host_key(client: Any, host: str, port: int) -> None:
    """Persist the key the server presented on a trusted connection."""
    try:
        transport = client.get_transport()
        if transport is not None:
            _remember_host_key(host, port, transport.get_remote_server_key())
    except Exception as exc:  # pragma: no cover - transport dependent
        logger.debug("[Remote] Could not read remote host key: %s", exc)


def _new_client(accept_new_host_key: bool) -> Any:
    """Build an SSHClient with the requested host key policy.

    Without an explicit opt-in, unknown host keys are rejected so that a
    man-in-the-middle cannot silently impersonate the target host.
    """
    import paramiko

    client = paramiko.SSHClient()
    _load_known_hosts(client)
    if accept_new_host_key:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    return client


def _open_jump_socket(
    jump_config: dict[str, Any],
    target_host: str,
    target_port: int,
    accept_new_host_key: bool,
) -> tuple[Any, Any]:
    """Connect to a jump host and open a direct-tcpip channel to the target."""
    import paramiko

    jump_client = _new_client(accept_new_host_key)
    try:
        jump_client.connect(
            **_build_connect_kwargs(
                hostname=jump_config["host"],
                port=jump_config["port"],
                username=jump_config["username"],
                password=jump_config.get("password", ""),
                key_path=jump_config.get("key_path", ""),
                passphrase=jump_config.get("passphrase", ""),
            )
        )
        transport = jump_client.get_transport()
        if transport is None or not transport.is_active():
            raise ConnectionError("Jump host SSH transport is closed")
        sock = transport.open_channel(
            "direct-tcpip",
            (target_host, target_port),
            ("", 0),
        )
    except paramiko.AuthenticationException as exc:
        jump_client.close()
        raise ConnectionError(
            "Jump host authentication failed for "
            f"{jump_config['username']}@{jump_config['host']}:{jump_config['port']}. "
            "Check your jump host username and password/key. "
            f"({exc})"
        ) from exc
    except Exception:
        jump_client.close()
        raise

    return jump_client, sock


def _probe_remote_platform(
    client: Any,
    timeout: float = 15.0,
) -> tuple[str, str, str]:
    """Detect ``(os_family, shell, os_name)`` over an open client."""
    try:
        returncode, stdout, _stderr, timed_out = _run_on_client(
            client, remote_platform.PLATFORM_PROBE_COMMAND, timeout
        )
    except Exception as exc:
        logger.warning(
            "[Remote] Platform probe failed (%s); assuming POSIX remote", exc
        )
        return remote_platform.POSIX, "", ""

    os_family, shell, os_name = remote_platform.parse_platform_probe(
        -1 if timed_out else returncode, stdout
    )
    if os_family == remote_platform.POSIX:
        return os_family, shell, os_name

    # Windows: the login shell decides the command grammar.
    shell = remote_platform.SHELL_CMD
    try:
        _rc, shell_out, _err, _to = _run_on_client(
            client, remote_platform.WINDOWS_SHELL_PROBE_COMMAND, timeout
        )
        shell = remote_platform.detect_windows_shell(shell_out)
    except Exception as exc:
        logger.warning(
            "[Remote] Windows shell probe failed (%s); assuming cmd.exe", exc
        )
    return remote_platform.WINDOWS, shell, "Windows"


def _host_key_error_message(exc: Exception) -> str | None:
    """Translate a paramiko host key rejection into an actionable message."""
    message = str(exc)
    if "known_hosts" not in message.lower():
        return None
    return (
        f"{message}\nThe remote host key is not trusted yet. Verify the host "
        "fingerprint out of band (for example `ssh-keyscan`), then trust it "
        "once: the profile's \"Trust Unknown Host Key\" switch, "
        "remote_connect(accept_new_host_key=True), or "
        "/remote connect accept_new_host_key=true. The key is then recorded in "
        f"{KNOWN_HOSTS_FILE} and later connections are verified against it."
    )


def _host_key_mismatch_message(host: str, port: int, exc: Exception) -> str:
    """Message for a key that differs from the recorded one."""
    return (
        f"Host key mismatch for {host}:{port} — the server presented a "
        "different key than the one recorded in known_hosts. This can mean a "
        "man-in-the-middle attack, or that the host was re-installed with new "
        "keys. Do not blindly trust it: verify the new fingerprint out of band, "
        f"then remove the stale entry from {KNOWN_HOSTS_FILE} (or your "
        f"~/.ssh/known_hosts) and connect again. ({exc})"
    )



class SSHManager:
    """Singleton manager for SSH connections keyed by session_id."""

    _instance: Optional["SSHManager"] = None

    def __new__(cls) -> "SSHManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._connections: dict[str, SSHConnectionInfo] = {}
            cls._instance._reconnect_params: dict[str, dict] = {}
            cls._instance._lock = asyncio.Lock()
            cls._instance._heartbeat_task: asyncio.Task | None = None
            cls._instance._env_cache: dict[str, RemoteEnvSnapshot] = {}
            cls._instance._ENV_TTL = 60.0  # seconds
            cls._instance._sudo_state: dict[str, SudoState] = {}
            #: session_id -> agent_id, learned by the chat middleware. Lets the
            #: management UI find a connection without knowing a session id
            #: (the settings route has no "current session").
            cls._instance._session_owners: dict[str, str] = {}
            #: agent_id -> connect kwargs, for connects requested before the
            #: chat had a session id (a brand-new chat has none yet).
            cls._instance._pending_connects: dict[str, dict[str, Any]] = {}
        return cls._instance

    # ── Deferred connects ───────────────────────────────────────────

    def set_pending_connect(self, agent_id: str, params: dict[str, Any]) -> None:
        """Remember a connect to fulfil once the chat has a session id."""
        if agent_id:
            self._pending_connects[agent_id] = params

    def pending_profile_id(self, agent_id: str) -> str:
        """Profile id of the caller's deferred connect, if any."""
        pending = self._pending_connects.get(agent_id)
        return str(pending.get("profile_id", "")) if pending else ""

    def clear_pending_connect(self, agent_id: str) -> None:
        self._pending_connects.pop(agent_id, None)

    async def materialize_pending(self, session_id: str, agent_id: str) -> bool:
        """Open the deferred connection for ``agent_id``, once.

        Called from the chat middleware, which is the first place that knows
        the session id of a newly created chat.
        """
        if not session_id or not agent_id:
            return False
        params = self._pending_connects.get(agent_id)
        if params is None:
            return False

        # Claim it before awaiting so concurrent requests cannot connect twice.
        self._pending_connects.pop(agent_id, None)
        if session_id in self._connections:
            return False
        try:
            await self.connect(
                session_id=session_id,
                owner=agent_id,
                **params,
            )
        except Exception as exc:
            logger.warning("[Remote] Deferred connect failed: %s", exc)
            return False
        logger.info("[Remote] Deferred connect established for %s", session_id)
        return True

    # ── Session ownership ───────────────────────────────────────────

    #: Upper bound for the session->agent map; oldest entries are dropped.
    _SESSION_OWNERS_LIMIT = 512

    def remember_session_owner(self, session_id: str, agent_id: str) -> None:
        """Record which agent a chat session belongs to."""
        if not session_id or not agent_id:
            return
        # Re-insert so the insertion order tracks recency.
        self._session_owners.pop(session_id, None)
        self._session_owners[session_id] = agent_id

        if len(self._session_owners) > self._SESSION_OWNERS_LIMIT:
            for key in list(self._session_owners)[: len(self._session_owners) // 2]:
                self._session_owners.pop(key, None)

    def resolve_session_for(self, subject: str) -> str | None:
        """Most recent session observed for ``subject`` (or any, if anonymous)."""
        session_ids = [
            sid
            for sid, owner in self._session_owners.items()
            if owner == subject
        ]
        if not session_ids and not subject:
            session_ids = list(self._session_owners.keys())
        if not session_ids:
            return None
        for sid in reversed(session_ids):
            if sid in self._connections:
                return sid
        return session_ids[-1]

    def find_active_connection(
        self,
        subject: str,
    ) -> tuple[str, SSHConnectionInfo] | None:
        """Return ``(session_id, connection)`` for the caller's connection.

        A connection is the caller's when it was opened by that caller
        (``owner``) or when it lives on a chat session the middleware observed
        for that caller. Anonymous callers (no identity header) match every
        unowned connection, which is the single-user case.
        """
        for sid, info in self._connections.items():
            if subject and info.owner == subject:
                return sid, info

        for sid in reversed(list(self._connections)):
            info = self._connections[sid]
            if info.owner and info.owner != subject:
                continue
            if subject and self._session_owners.get(sid, "") != subject:
                continue
            return sid, info

        if not subject:
            for sid, info in self._connections.items():
                if not info.owner:
                    return sid, info
        return None

    async def connect(
        self,
        session_id: str,
        host: str,
        port: int = 22,
        username: str = "root",
        password: str = "",
        key_path: str = "",
        passphrase: str = "",
        profile_id: str = "",
        jump_host_id: str = "",
        jump_name: str = "",
        jump_host: str = "",
        jump_port: int = 22,
        jump_username: str = "",
        jump_password: str = "",
        jump_key_path: str = "",
        jump_passphrase: str = "",
        accept_new_host_key: bool = False,
        owner: str = "",
    ) -> dict:
        """Establish an SSH connection and store it for the session.

        Returns connection info dict on success.
        Raises on failure with a clear error message.
        """
        import paramiko

        host, port = normalize_host_and_port(host, port)

        if not host:
            raise ValueError("host is required")
        if not username:
            raise ValueError("username is required")

        async with self._lock:
            # Disconnect existing connection for this session
            if session_id in self._connections:
                old = self._connections[session_id]
                try:
                    old.client.close()
                except Exception as exc:
                    logger.debug(
                        "[Remote] Error closing previous client: %s", exc
                    )
                del self._connections[session_id]
                logger.info(
                    "[Remote] Closed previous connection for session %s",
                    session_id,
                )

        jump_config = self._resolve_jump_config(
            jump_host_id=jump_host_id,
            jump_name=jump_name,
            jump_host=jump_host,
            jump_port=jump_port,
            jump_username=jump_username,
            jump_password=jump_password,
            jump_key_path=jump_key_path,
            jump_passphrase=jump_passphrase,
        )

        def _do_connect() -> tuple[paramiko.SSHClient, paramiko.SSHClient | None]:
            jump_client = None
            sock = None
            if jump_config:
                jump_client, sock = _open_jump_socket(
                    jump_config,
                    host,
                    port,
                    accept_new_host_key,
                )

            client = _new_client(accept_new_host_key)
            try:
                client.connect(
                    **_build_connect_kwargs(
                        hostname=host,
                        port=port,
                        username=username,
                        password=password,
                        key_path=key_path,
                        passphrase=passphrase,
                        sock=sock,
                    )
                )
            except Exception:
                client.close()
                if jump_client is not None:
                    jump_client.close()
                raise
            return client, jump_client

        try:
            client, jump_client = await asyncio.to_thread(_do_connect)
        except paramiko.BadHostKeyException as e:
            raise ConnectionError(
                _host_key_mismatch_message(host, port, e)
            ) from e
        except paramiko.AuthenticationException as e:
            raise ConnectionError(
                f"Authentication failed for {username}@{host}:{port}. "
                f"Check your username and password/key. ({e})"
            ) from e
        except paramiko.SSHException as e:
            via = (
                f" via jump host {jump_config['host']}:{jump_config['port']}"
                if jump_config
                else ""
            )
            raise ConnectionError(
                _host_key_error_message(e)
                or f"SSH error connecting to {host}:{port}{via}: {e}"
            ) from e
        except (OSError, ConnectionError, TimeoutError) as e:
            via = (
                f" via jump host {jump_config['host']}:{jump_config['port']}"
                if jump_config
                else ""
            )
            raise ConnectionError(
                f"Could not connect to {host}:{port}{via}: {e}"
            ) from e

        if accept_new_host_key:
            # Trust-on-first-use: remember the key so this host no longer
            # needs the override and later connections are still verified.
            await asyncio.to_thread(_remember_client_host_key, client, host, port)

        # Platform detection must happen before any command is wrapped so the
        # first user command already uses the right grammar.
        os_family, shell, os_name = await asyncio.to_thread(
            _probe_remote_platform, client
        )

        info = SSHConnectionInfo(
            client=client,
            host=host,
            port=port,
            username=username,
            jump_client=jump_client,
            jump_host_id=jump_config.get("id", "") if jump_config else "",
            jump_host_name=jump_config.get("name", "") if jump_config else "",
            jump_host=jump_config.get("host", "") if jump_config else "",
            jump_port=jump_config.get("port", 22) if jump_config else 22,
            jump_username=jump_config.get("username", "") if jump_config else "",
            profile_id=profile_id,
            owner=owner,
            os_family=os_family,
            accept_new_host_key=accept_new_host_key,
            remote_os=os_name,
            remote_shell=shell,
            _connect_params={
                "host": host,
                "port": port,
                "username": username,
                "password": password,
                "key_path": key_path,
                "passphrase": passphrase,
                "profile_id": profile_id,
                "accept_new_host_key": accept_new_host_key,
                "owner": owner,
                "jump_host_id": jump_config.get("id", "") if jump_config else "",
                "jump_name": jump_config.get("name", "") if jump_config else "",
                "jump_host": jump_config.get("host", "") if jump_config else "",
                "jump_port": jump_config.get("port", 22) if jump_config else 22,
                "jump_username": jump_config.get("username", "") if jump_config else "",
                "jump_password": jump_config.get("password", "") if jump_config else "",
                "jump_key_path": jump_config.get("key_path", "") if jump_config else "",
                "jump_passphrase": jump_config.get("passphrase", "") if jump_config else "",
            },
        )

        async with self._lock:
            self._connections[session_id] = info
            self._reconnect_params[session_id] = dict(info._connect_params)
            if self._heartbeat_task is None:
                self._start_heartbeat()

        logger.info(
            "[Remote] Connected: %s@%s:%d (session=%s)",
            username,
            host,
            port,
            session_id,
        )

        # Auto-detect remote environment in background
        asyncio.create_task(self._safe_detect_env(session_id))

        return info.to_dict()

    @staticmethod
    def _resolve_jump_config(
        *,
        jump_host_id: str = "",
        jump_name: str = "",
        jump_host: str = "",
        jump_port: int = 22,
        jump_username: str = "",
        jump_password: str = "",
        jump_key_path: str = "",
        jump_passphrase: str = "",
    ) -> dict[str, Any] | None:
        jump_host, jump_port = normalize_host_and_port(jump_host, jump_port)
        if jump_host:
            if not jump_username:
                raise ValueError("jump_username is required when jump_host is set")
            return {
                "id": "",
                "name": "",
                "host": jump_host,
                "port": jump_port,
                "username": jump_username,
                "password": jump_password,
                "key_path": jump_key_path,
                "passphrase": jump_passphrase,
            }

        jump_config = None
        if jump_host_id:
            jump_config = get_jump_host(jump_host_id)
            if jump_config is None:
                raise ValueError(f"jump host not found: {jump_host_id}")
        elif jump_name:
            jump_config = find_jump_host_by_name(jump_name)
            if jump_config is None:
                raise ValueError(f"jump host not found: {jump_name}")

        if jump_config is None:
            return None

        resolved = dict(jump_config)
        resolved["host"], resolved["port"] = normalize_host_and_port(
            str(resolved.get("host", "")),
            int(resolved.get("port", 22)),
        )
        return resolved

    async def disconnect(self, session_id: str) -> bool:
        """Disconnect the SSH session. Returns True if disconnected."""
        async with self._lock:
            info = self._connections.pop(session_id, None)
            self._reconnect_params.pop(session_id, None)
            self._sudo_state.pop(session_id, None)

        if info is None:
            return False

        try:
            info.client.close()
        except Exception:
            pass
        if info.jump_client is not None:
            try:
                info.jump_client.close()
            except Exception:
                pass

        logger.info(
            "[Remote] Disconnected %s@%s:%d (session=%s)",
            info.username,
            info.host,
            info.port,
            session_id,
        )
        return True

    async def disconnect_profile(self, profile_id: str) -> int:
        """Disconnect all sessions currently using the saved profile."""
        async with self._lock:
            matches = [
                (sid, info)
                for sid, info in self._connections.items()
                if info.profile_id == profile_id
            ]
            for sid, _info in matches:
                self._connections.pop(sid, None)
                self._reconnect_params.pop(sid, None)
                self._sudo_state.pop(sid, None)
                self._env_cache.pop(sid, None)

        for sid, info in matches:
            try:
                info.client.close()
            except Exception:
                pass
            if info.jump_client is not None:
                try:
                    info.jump_client.close()
                except Exception:
                    pass
            logger.info(
                "[Remote] Disconnected profile %s connection "
                "%s@%s:%d (session=%s)",
                profile_id,
                info.username,
                info.host,
                info.port,
                sid,
            )
        return len(matches)

    async def disconnect_jump_host(self, jump_host_id: str) -> int:
        """Disconnect all sessions currently using the saved jump host."""
        async with self._lock:
            matches = [
                (sid, info)
                for sid, info in self._connections.items()
                if info.jump_host_id == jump_host_id
            ]
            for sid, _info in matches:
                self._connections.pop(sid, None)
                self._reconnect_params.pop(sid, None)
                self._sudo_state.pop(sid, None)
                self._env_cache.pop(sid, None)

        for sid, info in matches:
            try:
                info.client.close()
            except Exception:
                pass
            if info.jump_client is not None:
                try:
                    info.jump_client.close()
                except Exception:
                    pass
            logger.info(
                "[Remote] Disconnected jump host %s connection "
                "%s@%s:%d (session=%s)",
                jump_host_id,
                info.username,
                info.host,
                info.port,
                sid,
            )
        return len(matches)

    def get_connection(self, session_id: str) -> Optional[SSHConnectionInfo]:
        """Get the active connection for a session, or None."""
        info = self._connections.get(session_id)
        if info is None:
            return None

        # Check transport health
        transport = info.client.get_transport()
        if transport is None or not transport.is_active():
            # Stale connection — remove it
            logger.warning(
                "[Remote] Stale connection detected for session %s, removing",
                session_id,
            )
            try:
                info.client.close()
            except Exception:
                pass
            if info.jump_client is not None:
                try:
                    info.jump_client.close()
                except Exception:
                    pass
            self._connections.pop(session_id, None)
            return None

        info.last_used = datetime.now(timezone.utc)
        return info

    def get_owner(self, session_id: str) -> str:
        """Return the auth subject that opened the session.

        Returns ``""`` when the session is unknown or was opened without an
        identifiable caller (single-user deployments).
        """
        info = self._connections.get(session_id)
        if info is not None:
            return info.owner
        params = self._reconnect_params.get(session_id)
        if params:
            return str(params.get("owner", ""))
        return ""

    def can_reconnect(self, session_id: str) -> bool:
        """True when cached parameters exist for a session with no live link.

        Used to tell "never connected" apart from "connection was lost".
        """
        return bool(self._reconnect_params.get(session_id))

    def list_connections_for(self, owner: str) -> list[dict]:
        """Return sanitized connections belonging to ``owner`` only."""
        result = []
        for sid, info in self._connections.items():
            if info.owner != owner:
                continue
            item = info.to_dict()
            item["session_id"] = sid
            result.append(item)
        return result

    async def execute_command(
        self,
        session_id: str,
        command: str,
        timeout: float = 60.0,
        cwd: Optional[str] = None,
        sudo: bool = False,
        sudo_password: str = "",
    ) -> tuple[int, str, str]:
        """Execute a command on the remote host.

        Returns (returncode, stdout, stderr).
        Raises ConnectionError if no active connection.
        """
        info = self.get_connection(session_id)
        if info is None:
            raise ConnectionError(
                f"No active SSH connection for session {session_id}"
            )

        # Resolve sudo password from state if not provided
        if sudo and not sudo_password:
            sudo_state = self._sudo_state.get(session_id)
            if sudo_state and sudo_state.password:
                sudo_password = sudo_state.password

        if sudo and remote_platform.is_windows(info.os_family):
            raise ConnectionError(
                "sudo is not available on Windows remotes. "
                "Run an elevated command explicitly instead."
            )

        # Wrap the command so it runs in the effective working directory. The
        # wrapper is platform specific; see remote/platform.py.
        effective_cwd = cwd or info.default_cwd
        inner_cmd = remote_platform.build_command(
            command,
            effective_cwd,
            info.os_family,
            info.remote_shell,
        )

        if sudo:
            if not sudo_password:
                raise ConnectionError(
                    "Sudo password not configured. "
                    "Use /remote set-sudo or POST /api/remote/connections/"
                    "{session_id}/sudo."
                )
            # sudo always needs a POSIX shell regardless of the login shell
            cmd = f"sudo -S -p '' sh -c {shlex.quote(inner_cmd)}"
            stdin_data = f"{sudo_password}\n"
        else:
            # The SSH server hands the string to the remote login shell, which
            # already owns quoting for its platform. No extra sh -c layer.
            cmd = inner_cmd
            stdin_data = ""

        logger.debug("[Remote] execute_command cmd=%r", cmd)

        def _do_exec() -> tuple[int, str, str]:
            _rc, stdout, stderr, timed_out = _run_on_client(
                info.client,
                cmd,
                timeout,
                stdin_data=stdin_data,
            )
            if timed_out:
                return (
                    -1,
                    stdout,
                    f"Command timed out after {timeout} seconds",
                )
            return _rc, stdout, stderr

        try:
            return await asyncio.to_thread(_do_exec)
        except ConnectionError:
            raise
        except Exception as e:
            raise ConnectionError(
                f"Remote command execution failed: {e}"
            ) from e

    # ── Sudo Management ─────────────────────────────────────────────

    def set_sudo(self, session_id: str, password: str, enabled: bool = True) -> None:
        """Configure sudo for a session."""
        self._sudo_state[session_id] = SudoState(
            enabled=enabled,
            password=password if enabled else "",
        )

    def clear_sudo(self, session_id: str) -> None:
        """Clear sudo configuration for a session."""
        self._sudo_state.pop(session_id, None)

    def get_sudo_state(self, session_id: str) -> dict:
        """Get sudo state for a session."""
        state = self._sudo_state.get(session_id)
        if state is None:
            return SudoState().to_dict()
        return state.to_dict()

    async def verify_sudo(self, session_id: str) -> dict:
        """Verify sudo access by running 'sudo -S -p '' true'.

        Returns dict with ok, verified_at, or error.
        """
        state = self._sudo_state.get(session_id)
        if state is None or not state.password:
            return {"ok": False, "error": "Sudo password not configured"}

        try:
            returncode, _, stderr = await self.execute_command(
                session_id,
                "true",
                timeout=10,
                sudo=True,
                sudo_password=state.password,
            )
            if returncode == 0:
                state.verified_at = datetime.now(timezone.utc)
                state.last_error = ""
                return {"ok": True, "verified_at": state.verified_at.isoformat()}
            else:
                error_msg = stderr.strip() or f"sudo returned exit code {returncode}"
                state.last_error = error_msg
                return {"ok": False, "error": error_msg}
        except Exception as e:
            state.last_error = str(e)
            return {"ok": False, "error": str(e)}

    async def close_all(self) -> None:
        """Close all SSH connections (called on shutdown)."""
        self._stop_heartbeat()
        async with self._lock:
            for sid, info in self._connections.items():
                try:
                    info.client.close()
                except Exception:
                    pass
                if info.jump_client is not None:
                    try:
                        info.jump_client.close()
                    except Exception:
                        pass
                logger.info(
                    "[Remote] Closed connection %s@%s:%d (session=%s)",
                    info.username,
                    info.host,
                    info.port,
                    sid,
                )
            self._connections.clear()
            self._reconnect_params.clear()
            self._sudo_state.clear()
            self._env_cache.clear()

    def list_connections(self) -> list[dict]:
        """Return sanitized info for all active connections."""
        result = []
        for sid, info in self._connections.items():
            d = info.to_dict()
            d["session_id"] = sid
            result.append(d)
        return result

    async def _verify_cwd(self, session_id: str, info: SSHConnectionInfo) -> bool:
        """Check that the default working directory is reachable."""
        probe = remote_platform.working_directory_probe(
            info.os_family, info.remote_shell
        )
        try:
            _rc, stdout, _stderr = await self.execute_command(
                session_id, probe, timeout=5
            )
        except Exception as exc:
            logger.debug("[Remote] Working directory probe failed: %s", exc)
            return False
        return bool(stdout.strip())

    # ── Heartbeat ────────────────────────────────────────────────────

    _HEARTBEAT_INTERVAL = 15.0
    _DEEP_CHECK_INTERVAL = 60.0
    _DEGRADED_THRESHOLD = 1
    _STALE_THRESHOLD = 2

    def _start_heartbeat(self) -> None:
        """Start a background heartbeat task to detect stale connections."""
        if self._heartbeat_task is not None:
            return

        async def _heartbeat_loop():
            tick = 0
            while True:
                await asyncio.sleep(self._HEARTBEAT_INTERVAL)
                tick += 1
                try:
                    deep = (tick * self._HEARTBEAT_INTERVAL) % self._DEEP_CHECK_INTERVAL < self._HEARTBEAT_INTERVAL
                    await self._check_all_connections(deep_check=deep)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("[Remote] Heartbeat check failed: %s", e)

                async with self._lock:
                    if not self._connections:
                        self._heartbeat_task = None
                        logger.debug("[Remote] Heartbeat stopped (no connections)")
                        return

        self._heartbeat_task = asyncio.create_task(_heartbeat_loop())
        logger.debug("[Remote] Heartbeat started (interval=%.1fs)", self._HEARTBEAT_INTERVAL)

    def _stop_heartbeat(self) -> None:
        """Stop the heartbeat task."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
            logger.debug("[Remote] Heartbeat stopped")

    async def _check_all_connections(self, deep_check: bool = False) -> None:
        """Check health of all active connections."""
        async with self._lock:
            session_ids = list(self._connections.keys())

        now = datetime.now(timezone.utc)
        stale: list[str] = []

        for sid in session_ids:
            info = self._connections.get(sid)
            if info is None:
                continue

            transport = info.client.get_transport()
            if transport is None or not transport.is_active():
                stale.append(sid)
                continue

            # Lightweight: send_ignore + measure RTT
            try:
                import time
                t0 = time.monotonic()
                await asyncio.to_thread(transport.send_ignore)
                rtt_ms = (time.monotonic() - t0) * 1000

                info.health.last_checked_at = now
                info.health.last_success_at = now
                info.health.latency_ms = round(rtt_ms, 1)
                info.health.last_error = ""

                if info.health.consecutive_failures > 0:
                    info.health.consecutive_failures = 0
                    info.health.status = "connected"
                    info.health.connected = True
            except Exception as e:
                info.health.consecutive_failures += 1
                info.health.last_checked_at = now
                info.health.last_error = str(e)
                if info.health.consecutive_failures >= self._STALE_THRESHOLD:
                    info.health.status = "stale"
                    stale.append(sid)
                elif info.health.consecutive_failures >= self._DEGRADED_THRESHOLD:
                    info.health.status = "degraded"

            # Deep check: verify cwd accessibility
            if deep_check and sid not in stale:
                info.health.cwd_ok = await self._verify_cwd(sid, info)

        if stale:
            async with self._lock:
                for sid in stale:
                    info = self._connections.pop(sid, None)
                    if info is not None:
                        info.health.status = "stale"
                        info.health.connected = False
                        info.health.reconnect_available = bool(
                            self._reconnect_params.get(sid)
                        )
                        try:
                            info.client.close()
                        except Exception:
                            pass
                        if info.jump_client is not None:
                            try:
                                info.jump_client.close()
                            except Exception:
                                pass
                        logger.warning(
                            "[Remote] Heartbeat: stale connection removed "
                            "%s@%s:%d (session=%s)",
                            info.username,
                            info.host,
                            info.port,
                            sid,
                        )

    # ── Auto Reconnect ──────────────────────────────────────────────

    async def auto_reconnect(self, session_id: str) -> dict:
        """Reconnect using cached connection parameters.

        Returns connection info dict on success.
        Raises on failure.
        """
        info = self._connections.get(session_id)
        params = dict(
            info._connect_params if info is not None
            else self._reconnect_params.get(session_id, {})
        )
        if not params:
            raise ConnectionError(
                "No cached connection parameters. Use remote_connect instead."
            )

        # Close stale connection if present
        if info is not None:
            try:
                info.client.close()
            except Exception:
                pass
            if info.jump_client is not None:
                try:
                    info.jump_client.close()
                except Exception:
                    pass
            self._connections.pop(session_id, None)

        return await self.connect(session_id=session_id, **params)

    # ── Health API ──────────────────────────────────────────────────

    def get_health(self, session_id: str) -> dict:
        """Get health status for a session's connection.

        Returns health dict. If connection is gone, returns stale status
        with reconnect availability.
        """
        info = self._connections.get(session_id)
        if info is not None:
            info.health.reconnect_available = True
            return info.health.to_dict()

        # Connection gone — check if reconnect is available
        has_params = bool(self._reconnect_params.get(session_id))
        return SSHHealthInfo(
            connected=False,
            status="disconnected",
            reconnect_available=has_params,
        ).to_dict()

    async def force_health_check(self, session_id: str) -> dict:
        """Force an immediate health check for a session.

        Returns updated health dict.
        """
        info = self._connections.get(session_id)
        if info is None:
            return self.get_health(session_id)

        now = datetime.now(timezone.utc)
        transport = info.client.get_transport()
        if transport is None or not transport.is_active():
            info.health.status = "stale"
            info.health.connected = False
            info.health.last_checked_at = now
            info.health.last_error = "SSH transport is closed"
            info.health.reconnect_available = bool(
                self._reconnect_params.get(session_id)
            )
            # Remove stale connection
            async with self._lock:
                self._connections.pop(session_id, None)
            try:
                info.client.close()
            except Exception:
                pass
            if info.jump_client is not None:
                try:
                    info.jump_client.close()
                except Exception:
                    pass
            return info.health.to_dict()

        # Measure RTT
        try:
            import time
            t0 = time.monotonic()
            await asyncio.to_thread(transport.send_ignore)
            rtt_ms = (time.monotonic() - t0) * 1000

            info.health.latency_ms = round(rtt_ms, 1)
            info.health.last_checked_at = now
            info.health.last_success_at = now
            info.health.last_error = ""
            info.health.consecutive_failures = 0
            info.health.status = "connected"
            info.health.connected = True
        except Exception as e:
            info.health.consecutive_failures += 1
            info.health.last_checked_at = now
            info.health.last_error = str(e)
            if info.health.consecutive_failures >= self._STALE_THRESHOLD:
                info.health.status = "stale"
                info.health.connected = False
                info.health.reconnect_available = bool(
                    self._reconnect_params.get(session_id)
                )
                async with self._lock:
                    self._connections.pop(session_id, None)
                try:
                    info.client.close()
                except Exception:
                    pass
                if info.jump_client is not None:
                    try:
                        info.jump_client.close()
                    except Exception:
                        pass
                return info.health.to_dict()
            elif info.health.consecutive_failures >= self._DEGRADED_THRESHOLD:
                info.health.status = "degraded"
                info.health.connected = True

        # Deep check: verify cwd
        info.health.cwd_ok = await self._verify_cwd(session_id, info)

        info.health.reconnect_available = True
        return info.health.to_dict()

    # ── Remote Environment Detection ────────────────────────────────

    _ENV_KEY_TO_FIELD = {
        "os": "remote_os",
        "arch": "remote_arch",
        "kernel": "remote_kernel",
        "shell": "remote_shell",
        "hostname": "hostname",
        "cpu": "cpu_cores",
        "memory": "memory",
        "disk_root": "disk_root",
    }

    async def get_remote_env(
        self, session_id: str, refresh: bool = False
    ) -> RemoteEnvSnapshot:
        """Get remote environment info, using cache when valid.

        If refresh=True or cache expired, re-detect from remote.
        """
        now = datetime.now(timezone.utc)

        if not refresh:
            cached = self._env_cache.get(session_id)
            if cached is not None and cached.detected_at is not None:
                age = (now - cached.detected_at).total_seconds()
                if age < self._ENV_TTL:
                    return cached

        snapshot = await self._detect_remote_env(session_id)
        self._env_cache[session_id] = snapshot
        return snapshot

    async def _detect_remote_env(self, session_id: str) -> RemoteEnvSnapshot:
        """Detect remote environment using a single platform-specific command."""
        info = self.get_connection(session_id)
        if info is None:
            return RemoteEnvSnapshot(last_error="No active connection")

        snapshot = RemoteEnvSnapshot(detected_at=datetime.now(timezone.utc))
        script = remote_platform.environment_script(
            info.os_family, info.remote_shell
        )

        try:
            returncode, stdout, stderr = await self.execute_command(
                session_id, script, timeout=15
            )
            if returncode != 0 and not stdout:
                snapshot.last_error = stderr.strip() or "Detection script failed"
                return snapshot

            tools: dict[str, bool] = {}
            detected_shell = ""
            for line in stdout.splitlines():
                line = line.strip()
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()

                if key.startswith("tool_"):
                    tools[key[5:]] = value == "1"
                elif key in self._ENV_KEY_TO_FIELD:
                    if key == "shell":
                        detected_shell = value
                    setattr(snapshot, self._ENV_KEY_TO_FIELD[key], value)

            # The scripts only report tools they found, so fill the gaps.
            snapshot.tools = {
                name: tools.get(name, False)
                for name in remote_platform.DETECTED_TOOLS
            }
            # Platform detection already established a shell; only trust the
            # probe when it produced something usable.
            if not detected_shell:
                snapshot.remote_shell = info.remote_shell

            # Also update connection info
            info.remote_os = snapshot.remote_os
            info.remote_arch = snapshot.remote_arch
            info.remote_kernel = snapshot.remote_kernel
            info.remote_shell = snapshot.remote_shell
            info.env_snapshot = snapshot
        except Exception as e:
            snapshot.last_error = str(e)

        return snapshot

    async def _safe_detect_env(self, session_id: str) -> None:
        """Detect remote env, swallowing errors."""
        try:
            await self.get_remote_env(session_id)
        except Exception as e:
            logger.debug("[Remote] Background env detection failed: %s", e)

    # ── Working Directory Management ────────────────────────────────

    async def test_connection(
        self,
        host: str,
        port: int = 22,
        username: str = "root",
        password: str = "",
        key_path: str = "",
        passphrase: str = "",
        jump_host_id: str = "",
        jump_name: str = "",
        jump_host: str = "",
        jump_port: int = 22,
        jump_username: str = "",
        jump_password: str = "",
        jump_key_path: str = "",
        jump_passphrase: str = "",
        accept_new_host_key: bool = False,
    ) -> dict:
        """Test an SSH connection without affecting current sessions.

        Returns dict with latency_ms, os_family, remote_os and remote_shell
        on success. Raises on failure.
        """
        import paramiko

        host, port = normalize_host_and_port(host, port)
        if not host:
            raise ValueError("host is required")
        if not username:
            raise ValueError("username is required")

        jump_config = self._resolve_jump_config(
            jump_host_id=jump_host_id,
            jump_name=jump_name,
            jump_host=jump_host,
            jump_port=jump_port,
            jump_username=jump_username,
            jump_password=jump_password,
            jump_key_path=jump_key_path,
            jump_passphrase=jump_passphrase,
        )

        def _do_test() -> dict:
            jump_client = None
            sock = None
            if jump_config:
                jump_client, sock = _open_jump_socket(
                    jump_config,
                    host,
                    port,
                    accept_new_host_key,
                )

            client = _new_client(accept_new_host_key)
            try:
                start = time.monotonic()
                client.connect(
                    **_build_connect_kwargs(
                        hostname=host,
                        port=port,
                        username=username,
                        password=password,
                        key_path=key_path,
                        passphrase=passphrase,
                        sock=sock,
                    )
                )
                latency_ms = round((time.monotonic() - start) * 1000, 1)
                if accept_new_host_key:
                    _remember_client_host_key(client, host, port)
                os_family, shell, os_name = _probe_remote_platform(
                    client, timeout=10
                )
                return {
                    "latency_ms": latency_ms,
                    "os_family": os_family,
                    "remote_os": os_name,
                    "remote_shell": shell,
                }
            finally:
                client.close()
                if jump_client is not None:
                    jump_client.close()

        try:
            return await asyncio.to_thread(_do_test)
        except paramiko.BadHostKeyException as e:
            raise ConnectionError(
                _host_key_mismatch_message(host, port, e)
            ) from e
        except paramiko.AuthenticationException as e:
            raise ConnectionError(
                f"Authentication failed for {username}@{host}:{port}. "
                f"({e})"
            ) from e
        except paramiko.SSHException as e:
            raise ConnectionError(
                _host_key_error_message(e)
                or f"SSH error connecting to {host}:{port}: {e}"
            ) from e
        except (OSError, ConnectionError, TimeoutError) as e:
            raise ConnectionError(
                f"Could not connect to {host}:{port}: {e}"
            ) from e

    async def set_default_cwd(
        self,
        session_id: str,
        cwd: str,
        verify: bool = True,
    ) -> dict:
        """Set the default remote working directory for a session.

        If verify=True, checks that the directory exists and is accessible.
        Returns dict with default_cwd and cwd_ok.
        Raises ConnectionError or ValueError on failure.
        """
        info = self.get_connection(session_id)
        if info is None:
            raise ConnectionError(
                f"No active SSH connection for session {session_id}"
            )

        if not cwd or not cwd.strip():
            raise ValueError("cwd path is required")

        cwd = cwd.strip()

        if verify:
            test_cmd = remote_platform.working_directory_probe(
                info.os_family, info.remote_shell
            )
            try:
                returncode, stdout, stderr = await self.execute_command(
                    session_id, test_cmd, timeout=5, cwd=cwd
                )
                if returncode != 0:
                    raise ValueError(
                        f"Directory not accessible: {cwd}\n{stderr.strip()}"
                    )
            except ConnectionError:
                raise
            except ValueError:
                raise
            except Exception as e:
                raise ValueError(
                    f"Failed to verify directory {cwd}: {e}"
                )

        info.default_cwd = cwd
        info.health.cwd_ok = True if verify else None
        logger.info(
            "[Remote] Default cwd set to %s (session=%s)",
            cwd,
            session_id,
        )
        return {
            "default_cwd": info.default_cwd,
            "cwd_ok": info.health.cwd_ok,
        }


def get_ssh_manager() -> SSHManager:
    """Get the SSHManager singleton."""
    return SSHManager()
