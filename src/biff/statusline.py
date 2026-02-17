"""Status line integration for Claude Code.

Provides install/uninstall for biff's status bar segment and the runtime
``biff statusline`` command that composes biff's unread count with the
user's original status line command.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from biff.relay import atomic_write

# Well-known paths ----------------------------------------------------------

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
MCP_CONFIG_PATH = Path.home() / ".claude.json"
STASH_PATH = Path.home() / ".biff" / "statusline-original.json"
UNREAD_DIR = Path.home() / ".biff" / "unread"

# Result types --------------------------------------------------------------


@dataclass(frozen=True)
class InstallResult:
    """Outcome of an install attempt."""

    installed: bool
    message: str


@dataclass(frozen=True)
class UninstallResult:
    """Outcome of an uninstall attempt."""

    uninstalled: bool
    message: str


# Settings I/O -------------------------------------------------------------


def read_settings(path: Path) -> dict[str, object]:
    """Read Claude Code ``settings.json``, returning ``{}`` if absent."""
    if not path.exists():
        return {}
    return json.loads(path.read_text())  # type: ignore[no-any-return]


def write_settings(path: Path, settings: dict[str, object]) -> None:
    """Atomic write of *settings* to *path*."""
    atomic_write(path, json.dumps(settings, indent=2) + "\n")


# Stash I/O -----------------------------------------------------------------


def read_stash(path: Path) -> str | dict[str, object] | None:
    """Read the stashed original ``statusLine`` value.

    Returns ``None`` on missing file or corrupt JSON so the runtime
    statusline command never crashes.
    """
    try:
        data = json.loads(path.read_text())
        return data.get("original")  # type: ignore[no-any-return]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def write_stash(path: Path, value: str | dict[str, object] | None) -> None:
    """Persist the original ``statusLine`` value to the stash file."""
    atomic_write(path, json.dumps({"original": value}) + "\n")


# Install / Uninstall -------------------------------------------------------


def _ensure_mcp_server(mcp_config_path: Path) -> None:
    """Ensure ``mcpServers.biff`` exists in the MCP config.

    Only writes the file when the entry is absent or differs from the
    expected value, so repeated calls are no-ops.
    """
    mcp_config = read_settings(mcp_config_path)
    servers = mcp_config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    expected = _biff_mcp_server_entry()
    if "biff" in servers and servers["biff"] == expected:
        return
    servers["biff"] = expected
    mcp_config["mcpServers"] = servers
    write_settings(mcp_config_path, mcp_config)


def install(
    settings_path: Path | None = None,
    stash_path: Path | None = None,
    mcp_config_path: Path | None = None,
) -> InstallResult:
    """Install biff into Claude Code's status bar and global MCP config.

    Stashes the current ``statusLine`` value, replaces it with the
    ``biff statusline`` command, and registers the biff MCP server in
    ``~/.claude/mcp.json``.

    If the statusline is already installed, the MCP server entry is
    still reconciled (idempotent) to support upgrades from older
    versions that did not register the MCP server.
    """
    if settings_path is None:
        settings_path = SETTINGS_PATH
    if stash_path is None:
        stash_path = STASH_PATH
    if mcp_config_path is None:
        mcp_config_path = MCP_CONFIG_PATH
    if stash_path.exists():
        _ensure_mcp_server(mcp_config_path)
        return InstallResult(installed=False, message="Already installed.")

    settings = read_settings(settings_path)
    original = settings.get("statusLine")
    write_stash(stash_path, original)  # type: ignore[arg-type]

    settings["statusLine"] = _biff_statusline_setting()
    write_settings(settings_path, settings)

    _ensure_mcp_server(mcp_config_path)

    return InstallResult(installed=True, message="Installed.")


def uninstall(
    settings_path: Path | None = None,
    stash_path: Path | None = None,
    mcp_config_path: Path | None = None,
) -> UninstallResult:
    """Remove biff from Claude Code's status bar and global MCP config.

    Restores the original ``statusLine`` value from the stash, deletes
    the stash file, and removes the biff MCP server from
    ``~/.claude/mcp.json``.
    """
    if settings_path is None:
        settings_path = SETTINGS_PATH
    if stash_path is None:
        stash_path = STASH_PATH
    if mcp_config_path is None:
        mcp_config_path = MCP_CONFIG_PATH
    if not stash_path.exists():
        return UninstallResult(uninstalled=False, message="Not installed.")

    original = read_stash(stash_path)
    settings = read_settings(settings_path)

    if original is None:
        settings.pop("statusLine", None)
    else:
        settings["statusLine"] = original

    write_settings(settings_path, settings)
    stash_path.unlink()

    mcp_config = read_settings(mcp_config_path)
    servers = mcp_config.get("mcpServers")
    if isinstance(servers, dict) and "biff" in servers:
        del servers["biff"]
        write_settings(mcp_config_path, mcp_config)

    return UninstallResult(uninstalled=True, message="Uninstalled.")


# Runtime -------------------------------------------------------------------


def run_statusline(
    stash_path: Path = STASH_PATH,
    unread_dir: Path = UNREAD_DIR,
) -> str:
    """Produce the status bar text for Claude Code.

    1. Read stdin (session JSON from Claude Code).
    2. If an original command is stashed, run it and capture its output.
    3. Read single PPID-keyed unread file for this session.
    4. Combine ``{original} | {biff_segment}`` with separator.
    """
    stdin_data = sys.stdin.read()
    original_cmd = _resolve_original_command(stash_path)
    original_output = _run_original(original_cmd, stdin_data) if original_cmd else ""
    unread = _read_session_unread(unread_dir / f"{os.getppid()}.json")
    biff = _biff_segment(unread)

    if original_output:
        return f"{original_output} | {biff}"
    return biff


# Helpers -------------------------------------------------------------------


def _resolve_biff_command() -> tuple[str, list[str]]:
    """Resolve the biff executable as ``(command, base_args)``.

    Prefers ``shutil.which("biff")``, falls back to
    ``sys.executable -m biff``.
    """
    which = shutil.which("biff")
    if which:
        return which, []
    return sys.executable, ["-m", "biff"]


def _biff_statusline_setting() -> dict[str, str]:
    """Build the ``statusLine`` settings object for Claude Code.

    Claude Code requires ``{"type": "command", "command": "..."}``.
    """
    cmd, base = _resolve_biff_command()
    parts = [cmd, *base, "statusline"]
    return {"type": "command", "command": " ".join(parts)}


def _biff_mcp_server_entry() -> dict[str, object]:
    """Build the MCP server entry for ``~/.claude/mcp.json``.

    Returns the ``command``/``args`` dict that Claude Code expects
    under ``mcpServers.<name>``.
    """
    cmd, base = _resolve_biff_command()
    return {
        "type": "stdio",
        "command": cmd,
        "args": [*base, "serve", "--transport", "stdio"],
    }


def _resolve_original_command(stash_path: Path) -> str | None:
    """Extract the shell command from the stashed ``statusLine`` value.

    Claude Code's schema requires ``{"type": "command", "command": "..."}``,
    so the stash is always ``None`` (no prior statusLine) or an object with
    a ``command`` key.
    """
    if not stash_path.exists():
        return None
    original = read_stash(stash_path)
    if original is None:
        return None
    if isinstance(original, dict):
        cmd = original.get("command")
        return cmd if isinstance(cmd, str) else None
    # Defensive: unexpected string form (schema requires object)
    return original


@dataclass(frozen=True)
class SessionUnread:
    """Unread state for a single session, parsed from a PPID-keyed file."""

    user: str
    count: int
    tty_name: str
    biff_enabled: bool = True


def _read_session_unread(path: Path) -> SessionUnread | None:
    """Read a PPID-keyed unread file, returning ``None`` on any error."""
    try:
        data = json.loads(path.read_text())
        biff_enabled = data.get("biff_enabled", True)
        return SessionUnread(
            user=str(data.get("user", "")),
            count=int(data.get("count", 0)),
            tty_name=str(data.get("tty_name", "")),
            biff_enabled=bool(biff_enabled),
        )
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None


def _biff_segment(unread: SessionUnread | None) -> str:
    """Format the biff status segment for a single session.

    No file → ``biff`` (plain fallback).
    Mesg off → ``user:tty(n)`` plain (regardless of actual count).
    Zero count → ``user:tty(0)`` plain.
    Nonzero → ``user:tty(N)`` bold yellow.
    """
    if unread is None:
        return "biff"
    name = unread.user or "biff"
    if not unread.biff_enabled:
        if unread.tty_name:
            return f"{name}:{unread.tty_name}(n)"
        return f"{name}(n)"
    if unread.tty_name:
        label = f"{name}:{unread.tty_name}({unread.count})"
    else:
        label = f"{name}({unread.count})"
    if unread.count == 0:
        return label
    return f"\033[1;33m{label}\033[0m"


def _run_original(command: str, stdin_data: str) -> str:
    """Run the original status line command, returning its stdout.

    Returns empty string on any failure (timeout, bad exit, etc.).
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""
