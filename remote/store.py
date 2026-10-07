"""Persistent storage for Remote SSH connection profiles."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from qwenpaw.constant import WORKING_DIR

_REMOTE_DIR = WORKING_DIR / "remote"
_PROFILES_FILE = _REMOTE_DIR / "profiles.json"
#: Host keys the user explicitly trusted through ``accept_new_host_key``.
#: Written in OpenSSH known_hosts format so later connections verify against
#: them instead of disabling verification.
KNOWN_HOSTS_FILE = _REMOTE_DIR / "known_hosts"


def _load_profiles_file() -> dict[str, Any]:
    if _PROFILES_FILE.is_file():
        try:
            return json.loads(_PROFILES_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"profiles": []}
    return {"profiles": []}


def _save_profiles_file(data: dict[str, Any]) -> None:
    """Write the store atomically, restricting permissions where possible.

    The file holds SSH passwords and passphrases, so it is written to a
    temporary sibling (mode 0600) and then atomically renamed into place.
    """
    _REMOTE_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp_path = _PROFILES_FILE.with_name(_PROFILES_FILE.name + ".tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:  # pragma: no cover - Windows / non-POSIX filesystems
        pass
    os.replace(tmp_path, _PROFILES_FILE)


def list_profiles() -> list[dict[str, Any]]:
    data = _load_profiles_file()
    profiles = data.get("profiles", [])
    if not isinstance(profiles, list):
        return []
    return [p for p in profiles if isinstance(p, dict)]


def list_jump_hosts() -> list[dict[str, Any]]:
    data = _load_profiles_file()
    jump_hosts = data.get("jump_hosts", [])
    if not isinstance(jump_hosts, list):
        return []
    return [j for j in jump_hosts if isinstance(j, dict)]


def create_profile(payload: dict[str, Any]) -> dict[str, Any]:
    data = _load_profiles_file()
    profiles = list_profiles()

    profile = _normalize_profile_payload(payload, profile_id=uuid4().hex)

    profiles.append(profile)
    data["profiles"] = profiles
    _save_profiles_file(data)
    return profile


def update_profile(
    profile_id: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    data = _load_profiles_file()
    profiles = list_profiles()

    updated_profile = None
    for index, profile in enumerate(profiles):
        if profile.get("id") != profile_id:
            continue

        merged = dict(profile)
        merged.update(
            _normalize_profile_payload(
                payload,
                profile_id=profile_id,
                existing=profile,
            )
        )
        profiles[index] = merged
        updated_profile = merged
        break

    if updated_profile is None:
        return None

    data["profiles"] = profiles
    _save_profiles_file(data)
    return updated_profile


def _normalize_profile_payload(
    payload: dict[str, Any],
    profile_id: str,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = _normalize_ssh_endpoint(payload, endpoint_id=profile_id)
    profile["name"] = (
        str(payload.get("name", "")).strip() or _default_name(payload)
    )
    profile["jump_host_id"] = str(payload.get("jump_host_id", "")).strip()
    profile["default_cwd"] = str(payload.get("default_cwd", "")).strip()
    profile["accept_new_host_key"] = bool(payload.get("accept_new_host_key", False))
    profile["sudo_password"] = str(payload.get("sudo_password", ""))

    if existing is not None:
        for secret in ("password", "passphrase", "sudo_password"):
            if not profile[secret]:
                profile[secret] = str(existing.get(secret, ""))

    return profile


def update_profile_cwd(profile_id: str, cwd: str) -> dict[str, Any] | None:
    """Update the default working directory of a profile.

    Deliberately narrow: a profile update replaces every field, so routing a
    cwd-only change through :func:`update_profile` would reset host,
    username, key path and jump host to their defaults.
    """
    data = _load_profiles_file()
    profiles = list_profiles()

    updated_profile = None
    for index, profile in enumerate(profiles):
        if profile.get("id") != profile_id:
            continue

        merged = dict(profile)
        merged["default_cwd"] = cwd.strip()
        profiles[index] = merged
        updated_profile = merged
        break

    if updated_profile is None:
        return None

    data["profiles"] = profiles
    _save_profiles_file(data)
    return updated_profile


def create_jump_host(payload: dict[str, Any]) -> dict[str, Any]:
    data = _load_profiles_file()
    jump_hosts = list_jump_hosts()

    jump_host = _normalize_jump_host_payload(payload, jump_host_id=uuid4().hex)

    jump_hosts.append(jump_host)
    data["jump_hosts"] = jump_hosts
    _save_profiles_file(data)
    return jump_host


def update_jump_host(
    jump_host_id: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    data = _load_profiles_file()
    jump_hosts = list_jump_hosts()

    updated_jump_host = None
    for index, jump_host in enumerate(jump_hosts):
        if jump_host.get("id") != jump_host_id:
            continue

        merged = dict(jump_host)
        merged.update(
            _normalize_jump_host_payload(
                payload,
                jump_host_id=jump_host_id,
                existing=jump_host,
            )
        )
        jump_hosts[index] = merged
        updated_jump_host = merged
        break

    if updated_jump_host is None:
        return None

    data["jump_hosts"] = jump_hosts
    _save_profiles_file(data)
    return updated_jump_host


def delete_jump_host(jump_host_id: str) -> bool:
    if any(p.get("jump_host_id") == jump_host_id for p in list_profiles()):
        raise ValueError("jump host is used by one or more profiles")

    data = _load_profiles_file()
    jump_hosts = list_jump_hosts()
    remaining = [j for j in jump_hosts if j.get("id") != jump_host_id]
    if len(remaining) == len(jump_hosts):
        return False
    data["jump_hosts"] = remaining
    _save_profiles_file(data)
    return True


def get_jump_host(jump_host_id: str) -> dict[str, Any] | None:
    for jump_host in list_jump_hosts():
        if jump_host.get("id") == jump_host_id:
            return jump_host
    return None


def find_jump_host_by_name(name: str) -> dict[str, Any] | None:
    needle = name.strip()
    if not needle:
        return None

    matches = [
        jump_host
        for jump_host in list_jump_hosts()
        if str(jump_host.get("name", "")).strip() == needle
    ]
    if len(matches) > 1:
        raise ValueError(
            f"Multiple jump hosts named {needle!r}; use jump_id instead"
        )
    return matches[0] if matches else None


def _normalize_jump_host_payload(
    payload: dict[str, Any],
    jump_host_id: str,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    jump_host = _normalize_ssh_endpoint(payload, endpoint_id=jump_host_id)
    jump_host["name"] = (
        str(payload.get("name", "")).strip() or _default_name(payload)
    )

    if existing is not None:
        if not jump_host["password"]:
            jump_host["password"] = str(existing.get("password", ""))
        if not jump_host["passphrase"]:
            jump_host["passphrase"] = str(existing.get("passphrase", ""))

    return jump_host


def _normalize_ssh_endpoint(
    payload: dict[str, Any],
    endpoint_id: str,
) -> dict[str, Any]:
    return {
        "id": endpoint_id,
        "host": str(payload.get("host", "")).strip(),
        "port": int(payload.get("port", 22)),
        "username": str(payload.get("username", "root")).strip() or "root",
        "password": str(payload.get("password", "")),
        "key_path": str(payload.get("key_path", "")).strip(),
        "passphrase": str(payload.get("passphrase", "")),
    }


def delete_profile(profile_id: str) -> bool:
    data = _load_profiles_file()
    profiles = list_profiles()
    remaining = [p for p in profiles if p.get("id") != profile_id]
    if len(remaining) == len(profiles):
        return False
    data["profiles"] = remaining
    _save_profiles_file(data)
    return True


def get_profile(profile_id: str) -> dict[str, Any] | None:
    for profile in list_profiles():
        if profile.get("id") == profile_id:
            return profile
    return None


def _default_name(payload: dict[str, Any]) -> str:
    username = str(payload.get("username", "root")).strip() or "root"
    host = str(payload.get("host", "")).strip() or "unknown-host"
    port = int(payload.get("port", 22))
    return f"{username}@{host}:{port}"
