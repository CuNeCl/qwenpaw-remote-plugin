"""Regression tests for profile persistence.

Covers the data-loss defect where a cwd-only update used to reset host,
username, key path, display name and jump host to their defaults.
"""

import json

import pytest

from remote import store as store_module


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "_REMOTE_DIR", tmp_path)
    monkeypatch.setattr(
        store_module, "_PROFILES_FILE", tmp_path / "profiles.json"
    )
    return store_module


PROFILE_PAYLOAD = {
    "name": "prod box",
    "host": "10.0.0.12",
    "port": 2222,
    "username": "deploy",
    "password": "s3cret",
    "key_path": "/home/me/.ssh/id_ed25519",
    "passphrase": "keypass",
    "sudo_password": "sudopass",
    "jump_host_id": "jump-1",
    "default_cwd": "/srv/app",
    "accept_new_host_key": True,
}


def test_create_and_list_profile(store):
    created = store.create_profile(dict(PROFILE_PAYLOAD))
    assert created["host"] == "10.0.0.12"
    assert created["accept_new_host_key"] is True
    assert created["sudo_password"] == "sudopass"

    listed = store.list_profiles()
    assert len(listed) == 1
    assert listed[0]["id"] == created["id"]


def test_update_profile_cwd_preserves_every_other_field(store):
    created = store.create_profile(dict(PROFILE_PAYLOAD))

    updated = store.update_profile_cwd(created["id"], "/srv/app/v2")
    assert updated is not None
    assert updated["default_cwd"] == "/srv/app/v2"

    # The regression: these fields must survive a cwd-only update.
    assert updated["host"] == "10.0.0.12"
    assert updated["port"] == 2222
    assert updated["username"] == "deploy"
    assert updated["key_path"] == "/home/me/.ssh/id_ed25519"
    assert updated["name"] == "prod box"
    assert updated["jump_host_id"] == "jump-1"
    assert updated["password"] == "s3cret"
    assert updated["passphrase"] == "keypass"
    assert updated["sudo_password"] == "sudopass"
    assert updated["accept_new_host_key"] is True

    persisted = store.list_profiles()[0]
    assert persisted == updated


def test_update_profile_cwd_unknown_id(store):
    assert store.update_profile_cwd("missing", "/tmp") is None


def test_update_profile_keeps_stored_secrets_when_omitted(store):
    created = store.create_profile(dict(PROFILE_PAYLOAD))

    payload = dict(PROFILE_PAYLOAD)
    payload["password"] = ""
    payload["passphrase"] = ""
    payload["sudo_password"] = ""
    updated = store.update_profile(created["id"], payload)

    assert updated is not None
    assert updated["password"] == "s3cret"
    assert updated["passphrase"] == "keypass"
    assert updated["sudo_password"] == "sudopass"


def test_store_write_is_atomic(store, tmp_path):
    store.create_profile(dict(PROFILE_PAYLOAD))
    assert (tmp_path / "profiles.json").is_file()
    assert not list(tmp_path.glob("*.tmp"))


def test_store_recovers_from_corrupt_file(store, tmp_path):
    (tmp_path / "profiles.json").write_text("{not json", encoding="utf-8")
    assert store.list_profiles() == []

    created = store.create_profile(dict(PROFILE_PAYLOAD))
    data = json.loads((tmp_path / "profiles.json").read_text(encoding="utf-8"))
    assert [p["id"] for p in data["profiles"]] == [created["id"]]


def test_jump_host_crud(store):
    jump_host = store.create_jump_host(
        {
            "name": "bastion",
            "host": "1.2.3.4",
            "port": 22,
            "username": "jump",
            "password": "jumpsecret",
        }
    )
    assert store.find_jump_host_by_name("bastion")["id"] == jump_host["id"]
    assert store.get_jump_host(jump_host["id"])["host"] == "1.2.3.4"

    payload = dict(PROFILE_PAYLOAD)
    payload["jump_host_id"] = jump_host["id"]
    store.create_profile(payload)

    # A jump host referenced by a profile cannot be deleted.
    with pytest.raises(ValueError):
        store.delete_jump_host(jump_host["id"])


def test_default_name_when_name_missing(store):
    created = store.create_profile(
        {"host": "example.com", "port": 22, "username": "root"}
    )
    assert created["name"] == "root@example.com:22"
