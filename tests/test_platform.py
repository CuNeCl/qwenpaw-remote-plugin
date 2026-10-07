"""Regression tests for the remote platform abstraction."""

import pytest

from remote import platform as p


# ── Detection ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "returncode, stdout, expected",
    [
        (0, "Linux\nshell=/bin/bash\n", (p.POSIX, "/bin/bash", "Linux")),
        (0, "Darwin\nshell=/bin/zsh\n", (p.POSIX, "/bin/zsh", "Darwin")),
        (1, "", (p.WINDOWS, "", "")),
        (9009, "'uname' is not recognized\n", (p.WINDOWS, "", "")),
        # Git Bash on PATH answers `uname` but cannot expand $SHELL.
        (0, "MINGW64_NT-10.0-19045\n", (p.WINDOWS, "", "")),
        (0, "Linux\nshell=\n", (p.WINDOWS, "", "")),
    ],
)
def test_parse_platform_probe(returncode, stdout, expected):
    assert p.parse_platform_probe(returncode, stdout) == expected


def test_detect_windows_shell():
    assert p.detect_windows_shell("%COMSPEC%") == p.SHELL_POWERSHELL
    assert p.detect_windows_shell("C:\\Windows\\system32\\cmd.exe") == p.SHELL_CMD
    assert p.detect_windows_shell("") == p.SHELL_CMD


# ── POSIX command wrapping ─────────────────────────────────────────────

def test_posix_no_cwd_returns_command():
    assert p.build_command("ls -la", "", p.POSIX, "/bin/bash") == "ls -la"
    assert p.build_command("ls -la", "/", p.POSIX, "/bin/bash") == "ls -la"


def test_posix_cwd_is_quoted():
    assert (
        p.build_command("ls", "/tmp/my dir", p.POSIX, "/bin/bash")
        == "cd '/tmp/my dir' && ls"
    )


def test_posix_tilde_is_expanded_through_home():
    # `cd '~/app'` never works: a tilde inside quotes is not expanded.
    assert p.build_command("ls", "~/app", p.POSIX, "/bin/bash") == 'cd "$HOME"/app && ls'
    assert p.build_command("ls", "~", p.POSIX, "/bin/bash") == 'cd "$HOME" && ls'
    assert (
        p.build_command("ls", "~/my app", p.POSIX, "/bin/bash")
        == 'cd "$HOME"\'/my app\' && ls'
    )


def test_posix_fish_uses_and_separator():
    assert (
        p.build_command("ls", "/tmp", p.POSIX, "/usr/bin/fish")
        == "cd /tmp; and ls"
    )


# ── Windows command wrapping ───────────────────────────────────────────

def test_windows_cmd_cwd():
    assert (
        p.build_command("dir", "C:\\Program Files", p.WINDOWS, p.SHELL_CMD)
        == 'cd /d "C:\\Program Files" && dir'
    )


def test_windows_cmd_without_cwd():
    assert p.build_command("dir", "", p.WINDOWS, p.SHELL_CMD) == "dir"


def test_windows_cmd_drops_embedded_quotes():
    assert p.quote_cmd_path('C:\\a"b') == '"C:\\ab"'


def test_windows_powershell_cwd():
    assert (
        p.build_command(
            "Get-ChildItem", "C:\\Program Files", p.WINDOWS, p.SHELL_POWERSHELL
        )
        == "Set-Location -LiteralPath 'C:\\Program Files'; Get-ChildItem"
    )


def test_windows_powershell_escapes_single_quote():
    assert p.quote_powershell_literal("C:\\it's") == "'C:\\it''s'"


def test_windows_powershell_without_cwd():
    assert p.build_command("dir", "", p.WINDOWS, p.SHELL_POWERSHELL) == "dir"


# ── Working directory probe ────────────────────────────────────────────

@pytest.mark.parametrize(
    "os_family, shell, expected",
    [
        (p.POSIX, "/bin/bash", "pwd"),
        (p.WINDOWS, p.SHELL_POWERSHELL, "(Get-Location).Path"),
        (p.WINDOWS, p.SHELL_CMD, "echo %CD%"),
    ],
)
def test_working_directory_probe(os_family, shell, expected):
    assert p.working_directory_probe(os_family, shell) == expected


# ── Environment detection scripts ──────────────────────────────────────

def test_posix_env_script():
    script = p.environment_script(p.POSIX, "/bin/bash")
    assert "uname -s" in script
    assert 'command -v "$t"' in script
    assert "tool_$t=1" in script


def test_cmd_env_script():
    script = p.environment_script(p.WINDOWS, p.SHELL_CMD)
    assert "echo os=Windows" in script
    assert "where git >nul 2>&1 && echo tool_git=1" in script
    # `>nul` is the cmd null device; /dev/null would create a file.
    assert "/dev/null" not in script


def test_powershell_env_script_has_no_double_quotes():
    script = p.environment_script(p.WINDOWS, p.SHELL_POWERSHELL)
    assert "Get-Command" in script
    assert "'os=Windows'" in script
    # The script is transported through the login shell, so it must stay
    # free of double quotes and line breaks.
    assert '"' not in script
    assert "\n" not in script


def test_detected_tools_are_covered_by_every_script():
    for os_family, shell in (
        (p.POSIX, "/bin/bash"),
        (p.WINDOWS, p.SHELL_CMD),
        (p.WINDOWS, p.SHELL_POWERSHELL),
    ):
        script = p.environment_script(os_family, shell)
        for tool in p.DETECTED_TOOLS:
            assert tool in script, f"{tool} missing from {os_family}/{shell} script"
