"""Request schemas for the Remote SSH REST API."""

from pydantic import BaseModel, Field


class ConnectRequest(BaseModel):
    """Request body for creating an SSH connection."""

    host: str = Field(..., description="Remote host IP or hostname")
    port: int = Field(default=22, ge=1, le=65535, description="SSH port")
    username: str = Field(default="root", description="SSH username")
    password: str = Field(default="", description="SSH password")
    key_path: str = Field(
        default="", description="Path to SSH private key file"
    )
    passphrase: str = Field(
        default="", description="Passphrase for the private key"
    )
    session_id: str = Field(
        ..., description="Session ID to associate with this connection"
    )
    profile_id: str = Field(
        default="", description="Saved connection profile ID"
    )
    jump_host_id: str = Field(default="", description="Saved jump host ID")
    jump_name: str = Field(default="", description="Saved jump host name")
    jump_host: str = Field(default="", description="Inline jump host")
    jump_port: int = Field(default=22, ge=1, le=65535, description="Jump SSH port")
    jump_username: str = Field(default="", description="Jump SSH username")
    jump_password: str = Field(default="", description="Jump SSH password")
    jump_key_path: str = Field(
        default="", description="Path to jump host SSH private key file"
    )
    jump_passphrase: str = Field(
        default="", description="Passphrase for the jump host private key"
    )
    default_cwd: str = Field(
        default="", description="Default remote working directory"
    )
    accept_new_host_key: bool = Field(
        default=False,
        description=(
            "Trust an unknown remote host key on first connect. "
            "Disabled by default to prevent man-in-the-middle attacks."
        ),
    )


class ProfileRequest(BaseModel):
    """Request body for creating a saved SSH profile."""

    name: str = Field(default="", description="Display name")
    host: str = Field(..., description="Remote host IP or hostname")
    port: int = Field(default=22, ge=1, le=65535, description="SSH port")
    username: str = Field(default="root", description="SSH username")
    password: str = Field(default="", description="SSH password")
    key_path: str = Field(
        default="", description="Path to SSH private key file"
    )
    passphrase: str = Field(
        default="", description="Passphrase for the private key"
    )
    sudo_password: str = Field(
        default="",
        description=(
            "Password used only for sudo. Never reuses the SSH login "
            "password. Leave empty to keep the stored value."
        ),
    )
    jump_host_id: str = Field(default="", description="Saved jump host ID")
    default_cwd: str = Field(
        default="", description="Default remote working directory"
    )
    accept_new_host_key: bool = Field(
        default=False,
        description=(
            "Trust an unknown remote host key on first connect. "
            "Disabled by default to prevent man-in-the-middle attacks."
        ),
    )


class JumpHostRequest(BaseModel):
    """Request body for creating a saved SSH jump host."""

    name: str = Field(default="", description="Display name")
    host: str = Field(..., description="Jump host IP or hostname")
    port: int = Field(default=22, ge=1, le=65535, description="SSH port")
    username: str = Field(default="root", description="SSH username")
    password: str = Field(default="", description="SSH password")
    key_path: str = Field(
        default="", description="Path to SSH private key file"
    )
    passphrase: str = Field(
        default="", description="Passphrase for the private key"
    )


class ExecRequest(BaseModel):
    """Request body for executing a command on the remote host."""

    command: str = Field(..., description="Shell command to execute")
    timeout: float = Field(
        default=60.0, ge=1, le=600, description="Timeout in seconds"
    )
    cwd: str = Field(default="", description="Remote working directory")


class CwdRequest(BaseModel):
    """Request body for setting the remote working directory."""

    cwd: str = Field(..., description="Remote working directory path")
    verify: bool = Field(
        default=True,
        description="Verify directory exists before setting",
    )


class ProfileTestRequest(BaseModel):
    """Request body for testing an SSH profile connection."""

    host: str = Field(..., description="Remote host IP or hostname")
    port: int = Field(default=22, ge=1, le=65535, description="SSH port")
    username: str = Field(default="root", description="SSH username")
    password: str = Field(default="", description="SSH password")
    key_path: str = Field(
        default="", description="Path to SSH private key file"
    )
    passphrase: str = Field(
        default="", description="Passphrase for the private key"
    )
    jump_host_id: str = Field(default="", description="Saved jump host ID")
    jump_name: str = Field(default="", description="Saved jump host name")
    jump_host: str = Field(default="", description="Inline jump host")
    jump_port: int = Field(
        default=22, ge=1, le=65535, description="Jump SSH port"
    )
    jump_username: str = Field(default="", description="Jump SSH username")
    jump_password: str = Field(default="", description="Jump SSH password")
    jump_key_path: str = Field(
        default="", description="Jump host private key path"
    )
    jump_passphrase: str = Field(
        default="", description="Jump host key passphrase"
    )
    accept_new_host_key: bool = Field(
        default=False,
        description="Trust an unknown remote host key on first connect.",
    )


class SudoConfigRequest(BaseModel):
    """Request body for configuring sudo."""

    password: str = Field(..., description="Sudo password")
    enabled: bool = Field(default=True, description="Enable sudo")
