"""Remote platform abstraction (POSIX / Windows).

This plugin can drive both POSIX and Windows remotes. Every
platform-specific detail lives here — probe commands, command wrapping,
path quoting and environment-detection scripts — so that ``SSHManager``
stays free of shell-specific branching.

Detection strategy (two probes, only for Windows):

1. ``uname -s`` succeeds  -> POSIX remote; the login shell is ``$SHELL``.
2. ``uname -s`` fails     -> Windows remote. ``echo %COMSPEC%`` is then
   used as a differential probe: ``cmd.exe`` expands the variable and
   prints a path, while PowerShell prints the token back verbatim.
"""

from __future__ import annotations

import shlex

POSIX = "posix"
WINDOWS = "windows"

SHELL_CMD = "cmd"
SHELL_POWERSHELL = "powershell"

#: Tools probed during environment detection. Both the POSIX and the
#: Windows scripts only emit a ``tool_<name>=1`` line when the tool is
#: present, so the parser fills the missing entries with ``False``.
DETECTED_TOOLS = (
    "git",
    "python3",
    "python",
    "node",
    "npm",
    "docker",
    "curl",
    "wget",
    "vim",
    "nano",
)

PLATFORM_PROBE_COMMAND = 'uname -s; echo "shell=$SHELL"'
WINDOWS_SHELL_PROBE_COMMAND = "echo %COMSPEC%"
#: Verbatim output produced by PowerShell (i.e. not expanded by cmd.exe).
WINDOWS_SHELL_PROBE_LITERAL = "%COMSPEC%"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def parse_platform_probe(
    returncode: int,
    stdout: str,
) -> tuple[str, str, str]:
    """Parse the platform probe into ``(os_family, shell, os_name)``.

    A POSIX remote answers ``uname -s`` *and* exposes ``$SHELL``. A Windows
    remote fails the probe — or, when a POSIX toolchain such as Git Bash is
    on PATH, answers ``uname`` but cannot expand ``$SHELL``.
    """
    os_name = ""
    shell = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("shell="):
            shell = line[len("shell="):].strip()
        elif not os_name:
            os_name = line

    if returncode == 0 and shell:
        return POSIX, shell, os_name
    return WINDOWS, "", ""


def detect_windows_shell(stdout: str) -> str:
    """Classify the Windows login shell from the ``echo %COMSPEC%`` probe."""
    if stdout.strip() == WINDOWS_SHELL_PROBE_LITERAL:
        return SHELL_POWERSHELL
    return SHELL_CMD


def is_windows(os_family: str) -> bool:
    return os_family == WINDOWS


# ---------------------------------------------------------------------------
# Quoting
# ---------------------------------------------------------------------------

def quote_posix_path(path: str) -> str:
    """Quote a POSIX path, keeping a leading tilde expandable.

    ``shlex.quote("~/app")`` yields ``'~/app'``, and a tilde inside single
    quotes is not expanded by the shell — so ``cd '~/app'`` always fails.
    """
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return f'"$HOME"{shlex.quote(path[1:])}'
    return shlex.quote(path)


def quote_powershell_literal(value: str) -> str:
    """Render a PowerShell single-quoted literal (backslashes are literal)."""
    return "'" + value.replace("'", "''") + "'"


def quote_cmd_path(path: str) -> str:
    """Render a double-quoted path for cmd.exe.

    ``cmd.exe`` has no escape sequence for a quote inside a quoted path, so
    embedded quotes are dropped rather than allowed to break out.
    """
    return '"' + path.replace('"', "") + '"'


# ---------------------------------------------------------------------------
# Command wrapping
# ---------------------------------------------------------------------------

def _build_posix_command(command: str, cwd: str, shell: str) -> str:
    if not cwd or cwd == "/":
        return command
    separator = "; and " if "fish" in (shell or "").lower() else " && "
    return f"cd {quote_posix_path(cwd)}{separator}{command}"


def _build_windows_command(command: str, cwd: str, shell: str) -> str:
    """Wrap a command so the remote Windows login shell can run it.

    The remote shell already owns the command's quoting, so the wrapper
    always uses that shell's own syntax.
    """
    if shell == SHELL_POWERSHELL:
        if not cwd:
            return command
        return f"Set-Location -LiteralPath {quote_powershell_literal(cwd)}; {command}"

    if not cwd:
        return command
    return f"cd /d {quote_cmd_path(cwd)} && {command}"


def build_command(command: str, cwd: str, os_family: str, shell: str) -> str:
    """Wrap ``command`` so it runs in ``cwd`` on the detected platform."""
    if is_windows(os_family):
        return _build_windows_command(command, cwd, shell)
    return _build_posix_command(command, cwd, shell)


def working_directory_probe(os_family: str, shell: str) -> str:
    """Command that prints the effective working directory."""
    if not is_windows(os_family):
        return "pwd"
    if shell == SHELL_POWERSHELL:
        return "(Get-Location).Path"
    return "echo %CD%"


# ---------------------------------------------------------------------------
# Environment detection scripts
# ---------------------------------------------------------------------------

_POSIX_ENV_SCRIPT = r"""printf 'os=%s\n' "$(uname -s 2>/dev/null)"
printf 'arch=%s\n' "$(uname -m 2>/dev/null)"
printf 'kernel=%s\n' "$(uname -r 2>/dev/null)"
printf 'shell=%s\n' "$SHELL"
printf 'hostname=%s\n' "$(hostname 2>/dev/null)"
printf 'cpu=%s\n' "$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo '')"
printf 'memory=%s\n' "$(free -h 2>/dev/null | awk '/^Mem:/{print $2}' || sysctl -n hw.memsize 2>/dev/null | awk '{printf "%.1fG", $1/1073741824}' || echo '')"
printf 'disk_root=%s\n' "$(df -h / 2>/dev/null | awk 'NR==2{print $2 " total, " $3 " used, " $4 " avail"}')"
for t in git python3 python node npm docker curl wget vim nano; do
  command -v "$t" >/dev/null 2>&1 && echo "tool_$t=1"
done"""

# The PowerShell script is handed to the remote login shell as a single
# `;`-separated line, so it must not contain line breaks. Only single quotes
# are used for string literals to keep the command transport-safe.
_POWERSHELL_TOOL_LIST = ", ".join(f"'{tool}'" for tool in DETECTED_TOOLS)

_POWERSHELL_ENV_SCRIPT = (
    "$ErrorActionPreference = 'SilentlyContinue';"
    " 'os=Windows';"
    " 'arch=' + $env:PROCESSOR_ARCHITECTURE;"
    " 'kernel=' + [System.Environment]::OSVersion.Version.ToString();"
    f" 'shell={SHELL_POWERSHELL}';"
    " 'hostname=' + $env:COMPUTERNAME;"
    " 'cpu=' + $env:NUMBER_OF_PROCESSORS;"
    " 'memory=' + [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB, 1) + 'G';"
    " $drive = Get-PSDrive -Name ($env:SystemDrive.TrimEnd(':'));"
    " if ($drive) { 'disk_root=' + [math]::Round($drive.Used/1GB, 1) + 'G used, '"
    " + [math]::Round($drive.Free/1GB, 1) + 'G avail' };"
    f" foreach ($tool in @({_POWERSHELL_TOOL_LIST}))"
    " { if (Get-Command $tool -ErrorAction SilentlyContinue) { 'tool_' + $tool + '=1' } }"
)

# cmd.exe evaluates `&`, `&&` and `||` left to right with equal precedence,
# so `A & B && C` means "run A, run B, run C only if B succeeded".
_CMD_ENV_SCRIPT = " & ".join(
    [
        "echo os=Windows",
        "echo arch=%PROCESSOR_ARCHITECTURE%",
        "echo kernel=%OS%",
        f"echo shell={SHELL_CMD}",
        "echo hostname=%COMPUTERNAME%",
        "echo cpu=%NUMBER_OF_PROCESSORS%",
    ]
    + [f"where {tool} >nul 2>&1 && echo tool_{tool}=1" for tool in DETECTED_TOOLS]
)


def environment_script(os_family: str, shell: str) -> str:
    """Return the environment-detection script for the remote platform."""
    if not is_windows(os_family):
        return _POSIX_ENV_SCRIPT
    if shell == SHELL_POWERSHELL:
        return _POWERSHELL_ENV_SCRIPT
    return _CMD_ENV_SCRIPT
