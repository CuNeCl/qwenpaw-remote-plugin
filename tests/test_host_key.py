"""Regression tests for host key trust handling."""

import pytest

from remote import ssh_manager


class FakeKey:
    """Minimal stand-in for a paramiko PKey."""

    def __init__(self, name="ssh-ed25519", b64="AAAAC3NzaC1lZDI1NTE5AAAAITESTKEY"):
        self._name = name
        self._b64 = b64

    def get_name(self):
        return self._name

    def get_base64(self):
        return self._b64


@pytest.fixture()
def known_hosts(tmp_path, monkeypatch):
    path = tmp_path / "known_hosts"
    monkeypatch.setattr(ssh_manager, "KNOWN_HOSTS_FILE", path)
    return path


def test_remember_host_key_writes_openssh_format(known_hosts):
    ssh_manager._remember_host_key("192.168.222.2", 22, FakeKey())

    content = known_hosts.read_text(encoding="utf-8")
    assert content.startswith("# Host keys trusted through the Remote SSH plugin")
    assert (
        "192.168.222.2 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTKEY\n" in content
    )


def test_remember_host_key_uses_bracketed_host_for_custom_port(known_hosts):
    ssh_manager._remember_host_key("10.0.0.5", 2222, FakeKey())
    content = known_hosts.read_text(encoding="utf-8")
    assert "[10.0.0.5]:2222 ssh-ed25519 " in content


def test_remember_host_key_is_idempotent(known_hosts):
    ssh_manager._remember_host_key("host.example", 22, FakeKey())
    ssh_manager._remember_host_key("host.example", 22, FakeKey())

    content = known_hosts.read_text(encoding="utf-8")
    assert content.count("host.example ssh-ed25519") == 1


def test_remember_host_key_ignores_missing_key(known_hosts):
    ssh_manager._remember_host_key("host.example", 22, None)
    assert not known_hosts.exists()


def test_host_key_error_message_only_matches_rejections():
    assert ssh_manager._host_key_error_message(
        RuntimeError("Authentication failed")
    ) is None

    message = ssh_manager._host_key_error_message(
        RuntimeError("Server '10.0.0.5' not found in known_hosts")
    )
    assert message is not None
    assert "accept_new_host_key" in message
    assert "known_hosts" in message


def test_host_key_mismatch_message_warns_about_mitm():
    message = ssh_manager._host_key_mismatch_message(
        "10.0.0.5", 22, RuntimeError("Host key does not match")
    )
    assert "mismatch" in message.lower()
    assert "man-in-the-middle" in message
