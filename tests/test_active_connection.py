"""Regression tests for owner-scoped active-connection resolution.

The Remote SSH settings route has no "current session", so the UI resolves a
connection (and the session id it needs) by caller identity instead.
"""

import pytest

from remote.ssh_manager import SSHConnectionInfo, get_ssh_manager


@pytest.fixture()
def manager():
    mgr = get_ssh_manager()
    mgr._connections.clear()
    mgr._session_owners.clear()
    mgr._reconnect_params.clear()
    yield mgr
    mgr._connections.clear()
    mgr._session_owners.clear()
    mgr._reconnect_params.clear()


def _connection(host="10.0.0.1", owner=""):
    return SSHConnectionInfo(
        client=object(),
        host=host,
        port=22,
        username="root",
        owner=owner,
    )


# ── session -> agent bookkeeping ───────────────────────────────────────

def test_resolve_session_is_scoped_to_agent(manager):
    manager.remember_session_owner("session-a", "agent-1")
    manager.remember_session_owner("session-b", "agent-2")

    assert manager.resolve_session_for("agent-1") == "session-a"
    assert manager.resolve_session_for("agent-2") == "session-b"
    assert manager.resolve_session_for("agent-3") is None


def test_resolve_session_prefers_the_most_recent(manager):
    manager.remember_session_owner("session-a", "agent-1")
    manager.remember_session_owner("session-b", "agent-1")
    assert manager.resolve_session_for("agent-1") == "session-b"

    # Re-touching an older session makes it the most recent again.
    manager.remember_session_owner("session-a", "agent-1")
    assert manager.resolve_session_for("agent-1") == "session-a"


def test_resolve_session_prefers_a_live_connection(manager):
    manager.remember_session_owner("session-a", "agent-1")
    manager.remember_session_owner("session-b", "agent-1")
    manager._connections["session-a"] = _connection()
    assert manager.resolve_session_for("agent-1") == "session-a"


def test_resolve_session_for_anonymous_caller_falls_back_to_any(manager):
    manager.remember_session_owner("session-a", "agent-1")
    assert manager.resolve_session_for("") == "session-a"


def test_remember_session_owner_ignores_incomplete_pairs(manager):
    manager.remember_session_owner("", "agent-1")
    manager.remember_session_owner("session-a", "")
    assert manager.resolve_session_for("agent-1") is None


def test_session_owner_map_is_bounded(manager):
    manager._SESSION_OWNERS_LIMIT = 4
    for index in range(10):
        manager.remember_session_owner(f"session-{index}", "agent-1")

    assert len(manager._session_owners) <= 4
    # The newest session must survive the trim.
    assert manager.resolve_session_for("agent-1") == "session-9"


# ── active connection lookup ───────────────────────────────────────────

def test_find_active_connection_by_owner(manager):
    manager._connections["page-session"] = _connection(owner="agent-1")
    found = manager.find_active_connection("agent-1")
    assert found is not None
    assert found[0] == "page-session"


def test_find_active_connection_via_chat_session(manager):
    # Connection opened by the agent inside a chat: owner is empty, but the
    # middleware recorded which agent the session belongs to.
    manager._connections["chat-session"] = _connection()
    manager.remember_session_owner("chat-session", "agent-1")

    found = manager.find_active_connection("agent-1")
    assert found is not None
    assert found[0] == "chat-session"

    # A different agent must not see it.
    assert manager.find_active_connection("agent-2") is None


def test_find_active_connection_ignores_other_owners(manager):
    manager._connections["other"] = _connection(owner="agent-2")
    assert manager.find_active_connection("agent-1") is None


def test_find_active_connection_anonymous_single_user(manager):
    manager._connections["only"] = _connection()
    found = manager.find_active_connection("")
    assert found is not None
    assert found[0] == "only"


def test_find_active_connection_returns_none_when_idle(manager):
    assert manager.find_active_connection("agent-1") is None


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


def test_active_endpoint_reports_connection_and_session(client, manager):
    manager._connections["chat-session"] = _connection()
    manager.remember_session_owner("chat-session", "agent-1")

    response = client.get(
        "/remote/connections/active",
        headers={"X-Agent-Id": "agent-1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "connection"
    assert body["session_id"] == "chat-session"
    assert body["connection"]["host"] == "10.0.0.1"
    assert body["health"]["status"] == "connected"


def test_active_endpoint_falls_back_to_recent_session(client, manager):
    manager.remember_session_owner("chat-session", "agent-1")

    response = client.get(
        "/remote/connections/active",
        headers={"X-Agent-Id": "agent-1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "recent-session"
    assert body["session_id"] == "chat-session"
    assert body["connection"] is None


def test_active_endpoint_without_identity(client, manager):
    manager.remember_session_owner("chat-session", "agent-1")

    response = client.get("/remote/connections/active")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "recent-session"
    assert body["session_id"] == "chat-session"


def test_active_endpoint_is_idle_without_any_session(client):
    response = client.get("/remote/connections/active")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "session_id": "",
        "connection": None,
        "health": None,
        "source": "none",
        "pending_profile_id": "",
    }


def test_active_endpoint_does_not_leak_another_agents_connection(client, manager):
    manager._connections["chat-session"] = _connection(host="secret-host")
    manager.remember_session_owner("chat-session", "agent-2")

    response = client.get(
        "/remote/connections/active",
        headers={"X-Agent-Id": "agent-1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["connection"] is None
    assert body["session_id"] == ""
