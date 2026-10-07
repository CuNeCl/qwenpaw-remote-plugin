"""REST API for SSH connection management."""

import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from ..ssh_manager import get_ssh_manager
from ..store import (
    create_jump_host,
    create_profile,
    delete_jump_host,
    delete_profile,
    get_jump_host,
    get_profile,
    list_jump_hosts,
    list_profiles,
    update_jump_host,
    update_profile,
    update_profile_cwd,
)
from .schemas import (
    ConnectRequest,
    CwdRequest,
    ExecRequest,
    JumpHostRequest,
    ProfileRequest,
    ProfileTestRequest,
    SudoConfigRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["remote"])


# --------------- Authorization helpers ---------------


def _caller_subject(request: Request) -> str:
    """Identify the caller from host-injected identity headers.

    The Console sends ``X-Agent-Id`` (the plugin adds it explicitly in
    ``ui/src/index.ts``). Deployments that provide no identity header
    (single-user local install) return ``""``, in which case ownership checks
    intentionally degrade to a no-op.
    """
    for header in ("x-agent-id", "x-user-id"):
        value = (request.headers.get(header) or "").strip()
        if value:
            return value
    return ""


def _assert_session_owner(session_id: str, request: Request) -> None:
    """Reject access to an SSH session opened by another caller.

    Sessions are keyed by an id the caller supplies, so without this check any
    authenticated caller could execute commands over somebody else's remote
    connection.
    """
    owner = get_ssh_manager().get_owner(session_id)
    if owner and owner != _caller_subject(request):
        raise HTTPException(
            status_code=403,
            detail="This SSH session belongs to another caller.",
        )


# --------------- Attempt throttling ---------------

_ATTEMPT_WINDOW = 60.0
_ATTEMPT_LIMIT = 20
_attempts: dict[str, list[float]] = {}


def _throttle_connect_attempts(request: Request) -> None:
    """Bound connect/test attempts per client to slow down credential guessing."""
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    stamps = [
        stamp
        for stamp in _attempts.get(client, [])
        if now - stamp < _ATTEMPT_WINDOW
    ]
    if len(stamps) >= _ATTEMPT_LIMIT:
        _attempts[client] = stamps
        raise HTTPException(
            status_code=429,
            detail="Too many connection attempts. Try again shortly.",
        )
    stamps.append(now)
    _attempts[client] = stamps

    if len(_attempts) > 1024:
        for key, values in list(_attempts.items()):
            if not values or now - values[-1] > _ATTEMPT_WINDOW:
                _attempts.pop(key, None)


# --------------- Endpoints ---------------


def _sanitize_secret_fields(item: dict) -> dict:
    response = dict(item)
    response["has_password"] = bool(response.pop("password", None))
    response["has_passphrase"] = bool(response.pop("passphrase", None))
    response["has_sudo_password"] = bool(response.pop("sudo_password", None))
    return response


def _jump_host_name_map() -> dict[str, str]:
    return {
        str(jump_host.get("id", "")): str(jump_host.get("name", ""))
        for jump_host in list_jump_hosts()
    }


def _validate_profile_jump_host(profile: dict) -> None:
    jump_host_id = str(profile.get("jump_host_id", "")).strip()
    if jump_host_id and get_jump_host(jump_host_id) is None:
        raise ValueError(f"jump host not found: {jump_host_id}")


@router.get("/connections/active")
async def get_active_connection(request: Request):
    """Resolve the caller's SSH connection without a session id.

    The Remote SSH settings route has no "current session", so the UI cannot
    supply one. Connections are matched by caller (``X-Agent-Id``) instead,
    and the session id needed by later scoped calls is returned here.
    """
    subject = _caller_subject(request)
    manager = get_ssh_manager()

    found = manager.find_active_connection(subject)
    if found is not None:
        session_id, conn = found
        info = conn.to_dict()
        info["session_id"] = session_id
        return {
            "session_id": session_id,
            "connection": info,
            "health": manager.get_health(session_id),
            "source": "connection",
            "pending_profile_id": "",
        }

    # No live connection. Still report the caller's most recent chat session so
    # the UI can attach a new connection to it.
    session_id = manager.resolve_session_for(subject)
    return {
        "session_id": session_id or "",
        "connection": None,
        "health": None,
        "source": "recent-session" if session_id else "none",
        "pending_profile_id": manager.pending_profile_id(subject),
    }


@router.get("/connections")
async def list_connections(
    request: Request,
    session_id: Optional[str] = None,
):
    """List the caller's SSH connections, optionally filtered by session_id."""
    manager = get_ssh_manager()
    subject = _caller_subject(request)

    if session_id:
        _assert_session_owner(session_id, request)
        conn = manager.get_connection(session_id)
        if conn is None:
            return {"connections": []}
        info = conn.to_dict()
        info["session_id"] = session_id
        return {"connections": [info]}

    return {"connections": manager.list_connections_for(subject)}


@router.get("/profiles")
async def get_profiles(request: Request, session_id: Optional[str] = None):
    """List saved SSH profiles with current connection state."""
    profiles = list_profiles()
    jump_host_names = _jump_host_name_map()
    active_profile_id = ""
    if session_id:
        _assert_session_owner(session_id, request)
        conn = get_ssh_manager().get_connection(session_id)
        active_profile_id = conn.profile_id if conn else ""

    result = []
    for profile in profiles:
        item = _sanitize_secret_fields(profile)
        item["connected"] = bool(
            active_profile_id and item.get("id") == active_profile_id
        )
        item["jump_host_name"] = jump_host_names.get(
            str(item.get("jump_host_id", "")),
            "",
        )
        result.append(item)

    return {
        "profiles": result,
        "active_profile_id": active_profile_id,
    }


@router.get("/jump-hosts")
async def get_jump_hosts():
    """List saved SSH jump hosts."""
    return {
        "jump_hosts": [
            _sanitize_secret_fields(jump_host)
            for jump_host in list_jump_hosts()
        ]
    }


@router.post("/jump-hosts")
async def create_jump_host_route(req: JumpHostRequest):
    """Create and persist an SSH jump host."""
    try:
        jump_host = create_jump_host(req.model_dump())
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _sanitize_secret_fields(jump_host)


@router.put("/jump-hosts/{jump_host_id}")
async def update_jump_host_route(jump_host_id: str, req: JumpHostRequest):
    """Update a persisted SSH jump host."""
    try:
        jump_host = update_jump_host(jump_host_id, req.model_dump())
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if jump_host is None:
        raise HTTPException(
            status_code=404,
            detail=f"Jump host not found: {jump_host_id}",
        )

    await get_ssh_manager().disconnect_jump_host(jump_host_id)

    return _sanitize_secret_fields(jump_host)


@router.delete("/jump-hosts/{jump_host_id}")
async def delete_jump_host_route(jump_host_id: str):
    """Delete a persisted SSH jump host."""
    try:
        deleted = delete_jump_host(jump_host_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Jump host not found: {jump_host_id}",
        )
    await get_ssh_manager().disconnect_jump_host(jump_host_id)
    return {"status": "ok", "jump_host_id": jump_host_id}


@router.post("/profiles")
async def create_profile_route(req: ProfileRequest):
    """Create and persist an SSH profile."""
    payload = req.model_dump()
    try:
        _validate_profile_jump_host(payload)
        profile = create_profile(payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _sanitize_secret_fields(profile)


@router.put("/profiles/{profile_id}")
async def update_profile_route(profile_id: str, req: ProfileRequest):
    """Update a persisted SSH profile."""
    payload = req.model_dump()
    try:
        _validate_profile_jump_host(payload)
        profile = update_profile(profile_id, payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if profile is None:
        raise HTTPException(
            status_code=404,
            detail=f"Profile not found: {profile_id}",
        )

    await get_ssh_manager().disconnect_profile(profile_id)

    return _sanitize_secret_fields(profile)


@router.delete("/profiles/{profile_id}")
async def delete_profile_route(profile_id: str):
    """Delete a persisted SSH profile."""
    await get_ssh_manager().disconnect_profile(profile_id)
    deleted = delete_profile(profile_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Profile not found: {profile_id}",
        )
    return {"status": "ok", "profile_id": profile_id}


@router.patch("/profiles/{profile_id}/cwd")
async def update_profile_cwd_route(profile_id: str, body: dict):
    """Update the default working directory of a saved profile.

    Only the cwd field is touched; the rest of the profile is preserved.
    """
    cwd = str(body.get("cwd", "")).strip()
    updated = update_profile_cwd(profile_id, cwd)
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail=f"Profile not found: {profile_id}",
        )
    return {
        "status": "ok",
        "profile_id": profile_id,
        "default_cwd": updated.get("default_cwd", ""),
    }


@router.post("/profiles/{profile_id}/connect")
async def connect_profile(profile_id: str, body: dict, request: Request):
    """Connect the current session using a saved SSH profile.

    ``body.accept_new_host_key`` may override the stored value for this
    attempt only, so the UI can offer a one-click "trust and retry" without
    weakening the saved profile.

    ``body.session_id`` may be empty: a brand-new chat has no session id until
    its first turn, so the request is recorded and fulfilled by the chat
    middleware once a session exists.
    """
    _throttle_connect_attempts(request)

    session_id = str(body.get("session_id", "")).strip()
    profile = get_profile(profile_id)
    if profile is None:
        raise HTTPException(
            status_code=404,
            detail=f"Profile not found: {profile_id}",
        )

    override = body.get("accept_new_host_key")
    accept_new_host_key = (
        bool(profile.get("accept_new_host_key", False))
        if override is None
        else bool(override)
    )

    manager = get_ssh_manager()

    jump_host_id = str(profile.get("jump_host_id", ""))
    connect_params = dict(
        host=str(profile.get("host", "")),
        port=int(profile.get("port", 22)),
        username=str(profile.get("username", "root")),
        password=str(profile.get("password", "")),
        key_path=str(profile.get("key_path", "")),
        passphrase=str(profile.get("passphrase", "")),
        profile_id=profile_id,
        jump_host_id=jump_host_id,
        accept_new_host_key=accept_new_host_key,
    )

    if not session_id:
        # Defer: the middleware fulfils this on the chat's first turn.
        subject = _caller_subject(request)
        if not subject:
            raise HTTPException(
                status_code=400,
                detail=(
                    "session_id is required when the caller has no identity; "
                    "send a chat message first"
                ),
            )
        manager.set_pending_connect(subject, connect_params)
        return {
            "pending": True,
            "profile_id": profile_id,
            "host": connect_params["host"],
        }

    try:
        if jump_host_id and get_jump_host(jump_host_id) is None:
            raise ValueError(f"jump host not found: {jump_host_id}")
        info = await manager.connect(
            session_id=session_id,
            owner=_caller_subject(request),
            **connect_params,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Set default_cwd from profile if specified
    profile_cwd = str(profile.get("default_cwd", "")).strip()
    if profile_cwd:
        try:
            await manager.set_default_cwd(
                session_id=session_id,
                cwd=profile_cwd,
                verify=False,
            )
        except Exception as exc:
            logger.debug("[Remote] Could not apply profile cwd: %s", exc)

    # Sudo uses its own dedicated credential — never the SSH login password.
    sudo_password = str(profile.get("sudo_password", ""))
    sudo_configured = False
    if sudo_password:
        manager.set_sudo(session_id, sudo_password, enabled=True)
        sudo_configured = True

    info["session_id"] = session_id
    info["sudo_configured"] = sudo_configured
    info["sudo_needs_password"] = not sudo_configured
    return info


@router.post("/connections")
async def create_connection(req: ConnectRequest, request: Request):
    """Create a new SSH connection."""
    _throttle_connect_attempts(request)
    manager = get_ssh_manager()

    try:
        info = await manager.connect(
            session_id=req.session_id,
            host=req.host,
            port=req.port,
            username=req.username,
            password=req.password,
            key_path=req.key_path,
            passphrase=req.passphrase,
            profile_id=req.profile_id,
            jump_host_id=req.jump_host_id,
            jump_name=req.jump_name,
            jump_host=req.jump_host,
            jump_port=req.jump_port,
            jump_username=req.jump_username,
            jump_password=req.jump_password,
            jump_key_path=req.jump_key_path,
            jump_passphrase=req.jump_passphrase,
            accept_new_host_key=req.accept_new_host_key,
            owner=_caller_subject(request),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if req.default_cwd.strip():
        try:
            await manager.set_default_cwd(
                session_id=req.session_id,
                cwd=req.default_cwd.strip(),
                verify=False,
            )
        except Exception as exc:
            logger.debug("[Remote] Could not apply default_cwd: %s", exc)

    info["session_id"] = req.session_id
    return info


@router.delete("/connections/{session_id}")
async def delete_connection(session_id: str, request: Request):
    """Disconnect an SSH session."""
    _assert_session_owner(session_id, request)

    manager = get_ssh_manager()
    disconnected = await manager.disconnect(session_id)

    if not disconnected:
        raise HTTPException(
            status_code=404,
            detail=f"No active connection for session {session_id}",
        )

    return {"status": "ok", "session_id": session_id}


@router.post("/connections/{session_id}/exec")
async def exec_command(session_id: str, req: ExecRequest, request: Request):
    """Execute a command on the remote host."""
    _assert_session_owner(session_id, request)

    manager = get_ssh_manager()

    try:
        returncode, stdout, stderr = await manager.execute_command(
            session_id=session_id,
            command=req.command,
            timeout=req.timeout,
            cwd=req.cwd or None,
        )
    except ConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


@router.get("/connections/{session_id}/status")
async def connection_status(session_id: str, request: Request):
    """Check connection health."""
    _assert_session_owner(session_id, request)

    manager = get_ssh_manager()
    health = manager.get_health(session_id)
    conn = manager.get_connection(session_id)

    if conn is None:
        return {"connected": False, "health": health}

    info = conn.to_dict()
    info["connected"] = True
    info["session_id"] = session_id
    info["health"] = health
    return info


@router.get("/connections/{session_id}/health")
async def get_connection_health(session_id: str, request: Request):
    """Get detailed health status for a session's connection."""
    _assert_session_owner(session_id, request)

    manager = get_ssh_manager()
    return {
        "session_id": session_id,
        "health": manager.get_health(session_id),
    }


@router.post("/connections/{session_id}/health/check")
async def force_health_check(session_id: str, request: Request):
    """Force an immediate health check."""
    _assert_session_owner(session_id, request)

    manager = get_ssh_manager()
    health = await manager.force_health_check(session_id)
    return {
        "session_id": session_id,
        "health": health,
    }


@router.post("/connections/{session_id}/reconnect")
async def reconnect_session(session_id: str, request: Request):
    """Reconnect using cached connection parameters."""
    _assert_session_owner(session_id, request)

    manager = get_ssh_manager()
    try:
        info = await manager.auto_reconnect(session_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))

    info["session_id"] = session_id
    return info


@router.get("/connections/{session_id}/cwd")
async def get_cwd(session_id: str, request: Request):
    """Get the current default working directory for a session."""
    _assert_session_owner(session_id, request)

    manager = get_ssh_manager()
    conn = manager.get_connection(session_id)
    if conn is None:
        return {"session_id": session_id, "default_cwd": "/", "cwd_ok": None}
    return {
        "session_id": session_id,
        "default_cwd": conn.default_cwd,
        "cwd_ok": conn.health.cwd_ok if conn.health else None,
    }


@router.put("/connections/{session_id}/cwd")
async def set_cwd(session_id: str, req: CwdRequest, request: Request):
    """Set the default working directory for a session."""
    _assert_session_owner(session_id, request)

    manager = get_ssh_manager()
    try:
        result = await manager.set_default_cwd(
            session_id=session_id,
            cwd=req.cwd,
            verify=req.verify,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))

    result["session_id"] = session_id
    return result


@router.get("/connections/{session_id}/info")
async def get_remote_info(
    session_id: str,
    request: Request,
    refresh: bool = False,
):
    """Get cached remote environment info."""
    _assert_session_owner(session_id, request)

    manager = get_ssh_manager()
    snapshot = await manager.get_remote_env(session_id, refresh=refresh)
    return {
        "session_id": session_id,
        "info": snapshot.to_dict(),
    }


@router.post("/connections/{session_id}/info/refresh")
async def refresh_remote_info(session_id: str, request: Request):
    """Force refresh remote environment info."""
    _assert_session_owner(session_id, request)

    manager = get_ssh_manager()
    snapshot = await manager.get_remote_env(session_id, refresh=True)
    return {
        "session_id": session_id,
        "info": snapshot.to_dict(),
    }


@router.post("/profiles/test")
async def test_profile_connection(req: ProfileTestRequest, request: Request):
    """Test an SSH profile connection without affecting current session."""
    _throttle_connect_attempts(request)

    manager = get_ssh_manager()
    try:
        result = await manager.test_connection(
            host=req.host,
            port=req.port,
            username=req.username,
            password=req.password,
            key_path=req.key_path,
            passphrase=req.passphrase,
            jump_host_id=req.jump_host_id,
            jump_name=req.jump_name,
            jump_host=req.jump_host,
            jump_port=req.jump_port,
            jump_username=req.jump_username,
            jump_password=req.jump_password,
            jump_key_path=req.jump_key_path,
            jump_passphrase=req.jump_passphrase,
            accept_new_host_key=req.accept_new_host_key,
        )
    except (ConnectionError, ValueError) as e:
        return {"ok": False, "error": str(e)}

    return {"ok": True, **result}


@router.post("/profiles/{profile_id}/test")
async def test_saved_profile(profile_id: str, request: Request):
    """Test a saved SSH profile connection."""
    _throttle_connect_attempts(request)

    profile = get_profile(profile_id)
    if profile is None:
        raise HTTPException(
            status_code=404,
            detail=f"Profile not found: {profile_id}",
        )

    manager = get_ssh_manager()
    jump_host_id = str(profile.get("jump_host_id", ""))
    if jump_host_id and get_jump_host(jump_host_id) is None:
        raise HTTPException(
            status_code=400,
            detail=f"Jump host not found: {jump_host_id}",
        )

    try:
        result = await manager.test_connection(
            host=str(profile.get("host", "")),
            port=int(profile.get("port", 22)),
            username=str(profile.get("username", "root")),
            password=str(profile.get("password", "")),
            key_path=str(profile.get("key_path", "")),
            passphrase=str(profile.get("passphrase", "")),
            jump_host_id=jump_host_id,
            accept_new_host_key=bool(profile.get("accept_new_host_key", False)),
        )
    except (ConnectionError, ValueError) as e:
        return {"ok": False, "error": str(e)}

    return {"ok": True, **result}


@router.post("/connections/{session_id}/sudo")
async def configure_sudo(
    session_id: str,
    req: SudoConfigRequest,
    request: Request,
):
    """Configure sudo for a session."""
    _assert_session_owner(session_id, request)

    manager = get_ssh_manager()
    manager.set_sudo(session_id, req.password, req.enabled)
    return {"status": "ok", "session_id": session_id}


@router.get("/connections/{session_id}/sudo")
async def get_sudo_state(session_id: str, request: Request):
    """Get sudo state for a session."""
    _assert_session_owner(session_id, request)

    manager = get_ssh_manager()
    return {
        "session_id": session_id,
        "sudo": manager.get_sudo_state(session_id),
    }


@router.post("/connections/{session_id}/sudo/verify")
async def verify_sudo(session_id: str, request: Request):
    """Verify sudo access."""
    _assert_session_owner(session_id, request)

    manager = get_ssh_manager()
    result = await manager.verify_sudo(session_id)
    result["session_id"] = session_id
    return result
