"""Route-matching regression tests for the plugin REST API.

A static path that is declared after a parameterized one can be shadowed, in
which case the static endpoint answers 405 instead of reaching its handler.
These tests pin the resolution of every profile route so a future reordering
cannot reintroduce that class of bug.
"""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from remote.routers.connections import router


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(router, prefix="/remote")
    return TestClient(app)


@pytest.mark.parametrize(
    "method, path, expected",
    [
        # Static routes must resolve. 422 means the handler ran and rejected
        # the empty body, i.e. the route was matched; a 405 would mean it was
        # shadowed by a parameterized route.
        ("POST", "/remote/profiles/test", 422),
        ("POST", "/remote/profiles", 422),
        ("PUT", "/remote/profiles/abc", 422),
        ("POST", "/remote/connections", 422),
        # Parameterized routes resolve as well.
        ("POST", "/remote/profiles/abc/test", 404),
        ("PATCH", "/remote/profiles/abc/cwd", 404),
        ("DELETE", "/remote/profiles/abc", 404),
        # Unknown profile wins over the missing session_id check.
        ("POST", "/remote/profiles/abc/connect", 404),
    ],
)
def test_route_resolution(client, method, path, expected):
    response = client.request(method, path, json={})
    assert response.status_code == expected, (
        f"{method} {path} -> {response.status_code} {response.text[:120]}"
    )


def test_static_test_route_is_not_get(client):
    """A GET on the POST-only test route is the 405 users reported.

    Documented here so the symptom is recognisable: it means the caller sent
    GET instead of POST, not that the route is missing.
    """
    response = client.get("/remote/profiles/test")
    assert response.status_code == 405


def test_session_scoped_routes_require_ownership_check(client):
    """Session-scoped routes must not 500 on an unknown session id."""
    response = client.post(
        "/remote/connections/unknown-session/exec",
        json={"command": "true"},
    )
    assert response.status_code == 502
    assert "No active SSH connection" in response.json()["detail"]
