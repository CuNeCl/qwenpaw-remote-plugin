"""Regression tests for connects requested before a chat has a session id.

A brand-new chat has no session id until its first turn, so the settings page
cannot attach a connection to it. The request is recorded per agent and
fulfilled by the chat middleware on the first turn.
"""

import asyncio

import pytest

from remote import store
from remote.ssh_manager import get_ssh_manager


@pytest.fixture()
def manager():
    mgr = get_ssh_manager()
    mgr._connections.clear()
    mgr._session_owners.clear()
    mgr._pending_connects.clear()
    mgr._reconnect_params.clear()
    yield mgr
    mgr._connections.clear()
    mgr._session_owners.clear()
    mgr._pending_connects.clear()
    mgr._reconnect_params.clear()


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_PROFILES_FILE", tmp_path / "profiles.json")
    return store.create_profile(
        {"host": "10.0.0.9", "port": 22, "username": "root", "name": "win-box"}
    )


# ── manager ────────────────────────────────────────────────────────────

def test_pending_connect_is_recorded_and_reported(manager):
    manager.set_pending_connect("agent-1", {"host": "10.0.0.9", "profile_id": "p1"})

    assert manager.pending_profile_id("agent-1") == "p1"
    assert manager.pending_profile_id("agent-2") == ""
    assert manager.pending_profile_id("") == ""


def test_pending_connect_ignores_anonymous_callers(manager):
    manager.set_pending_connect("", {"host": "10.0.0.9", "profile_id": "p1"})
    assert manager.pending_profile_id("") == ""


def test_materialize_opens_the_deferred_connection(manager, monkeypatch):
    manager.set_pending_connect(
        "agent-1",
        {"host": "10.0.0.9", "port": 22, "username": "root", "profile_id": "p1"},
    )

    calls = []

    async def fake_connect(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(manager, "connect", fake_connect)

    assert asyncio.run(manager.materialize_pending("new-session", "agent-1")) is True
    assert len(calls) == 1
    assert calls[0]["session_id"] == "new-session"
    assert calls[0]["owner"] == "agent-1"
    assert calls[0]["host"] == "10.0.0.9"

    # Claimed exactly once.
    assert manager.pending_profile_id("agent-1") == ""
    assert asyncio.run(manager.materialize_pending("other", "agent-1")) is False


def test_materialize_is_a_noop_without_a_pending_request(manager):
    assert asyncio.run(manager.materialize_pending("session", "agent-1")) is False
    assert asyncio.run(manager.materialize_pending("", "agent-1")) is False
    assert asyncio.run(manager.materialize_pending("session", "")) is False


def test_materialize_reports_failures_without_raising(manager, monkeypatch):
    manager.set_pending_connect("agent-1", {"host": "h", "profile_id": "p1"})

    async def failing_connect(**kwargs):
        raise ConnectionError("unreachable")

    monkeypatch.setattr(manager, "connect", failing_connect)
    assert asyncio.run(manager.materialize_pending("session", "agent-1")) is False


# ── route ──────────────────────────────────────────────────────────────

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from remote.routers.connections import router  # noqa: E402


@pytest.fixture()
def client(manager):
    app = FastAPI()
    app.include_router(router, prefix="/remote")
    return TestClient(app)


def test_connect_without_session_defers(client, manager, profile):
    response = client.post(
        f"/remote/profiles/{profile['id']}/connect",
        json={"session_id": ""},
        headers={"X-Agent-Id": "agent-1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["pending"] is True
    assert body["profile_id"] == profile["id"]
    assert manager.pending_profile_id("agent-1") == profile["id"]


def test_active_endpoint_reports_the_pending_profile(client, manager, profile):
    client.post(
        f"/remote/profiles/{profile['id']}/connect",
        json={"session_id": ""},
        headers={"X-Agent-Id": "agent-1"},
    )

    body = client.get(
        "/remote/connections/active",
        headers={"X-Agent-Id": "agent-1"},
    ).json()
    assert body["pending_profile_id"] == profile["id"]
    assert body["connection"] is None


def test_connect_without_session_needs_an_identity(client, profile):
    response = client.post(
        f"/remote/profiles/{profile['id']}/connect",
        json={"session_id": ""},
    )
    assert response.status_code == 400
    assert "session_id is required" in response.json()["detail"]


def test_connect_with_session_still_connects_directly(client, manager, profile, monkeypatch):
    calls = []

    async def fake_connect(**kwargs):
        calls.append(kwargs)
        return {"host": kwargs["host"]}

    monkeypatch.setattr(manager, "connect", fake_connect)

    response = client.post(
        f"/remote/profiles/{profile['id']}/connect",
        json={"session_id": "chat-session"},
        headers={"X-Agent-Id": "agent-1"},
    )
    assert response.status_code == 200
    assert calls and calls[0]["session_id"] == "chat-session"
    assert manager.pending_profile_id("agent-1") == ""
