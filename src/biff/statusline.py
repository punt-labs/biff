"""Status line integration for Claude Code.

Provides install/uninstall for biff's status bar segment and the runtime
``biff statusline`` command that composes biff's unread count with the
user's original status line command.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from biff.relay import atomic_write

# Well-known paths ----------------------------------------------------------

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
STASH_PATH = Path.home() / ".biff" / "statusline-original.json"
UNREAD_PATH = Path.home() / ".biff" / "unread.json"

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


def install(
    settings_path: Path | None = None,
    stash_path: Path | None = None,
) -> InstallResult:
    """Install biff into Claude Code's status bar.

    Stashes the current ``statusLine`` value and replaces it with the
    ``biff statusline`` command.
    """
    if settings_path is None:
        settings_path = SETTINGS_PATH
    if stash_path is None:
        stash_path = STASH_PATH
    if stash_path.exists():
        return InstallResult(installed=False, message="Already installed.")

    settings = read_settings(settings_path)
    original = settings.get("statusLine")
    write_stash(stash_path, original)  # type: ignore[arg-type]

    settings["statusLine"] = _biff_statusline_setting()
    write_settings(settings_path, settings)

    return InstallResult(installed=True, message="Installed.")


def uninstall(
    settings_path: Path | None = None,
    stash_path: Path | None = None,
) -> UninstallResult:
    """Remove biff from Claude Code's status bar.

    Restores the original ``statusLine`` value from the stash and deletes
    the stash file.
    """
    if settings_path is None:
        settings_path = SETTINGS_PATH
    if stash_path is None:
        stash_path = STASH_PATH
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

    return UninstallResult(uninstalled=True, message="Uninstalled.")


# Runtime -------------------------------------------------------------------


def run_statusline(
    stash_path: Path = STASH_PATH,
    unread_path: Path = UNREAD_PATH,
) -> str:
    """Produce the status bar text for Claude Code.

    1. Read stdin (session JSON from Claude Code).
    2. If an original command is stashed, run it and capture its output.
    3. Read unread count from ``unread.json``.
    4. Combine ``{original} | biff(N)`` with separator.
    """
    stdin_data = sys.stdin.read()
    original_cmd = _resolve_original_command(stash_path)
    original_output = _run_original(original_cmd, stdin_data) if original_cmd else ""
    count = _read_unread_count(unread_path)
    biff = _biff_segment(count)

    if original_output:
        return f"{original_output} | {biff}"
    return biff


# Helpers -------------------------------------------------------------------


def _biff_statusline_setting() -> dict[str, str]:
    """Build the ``statusLine`` settings object for Claude Code.

    Claude Code requires ``{"type": "command", "command": "..."}``.
    Prefers ``shutil.which("biff")``, falls back to
    ``sys.executable -m biff``.
    """
    which = shutil.which("biff")
    cmd = f"{which} statusline" if which else f"{sys.executable} -m biff statusline"
    return {"type": "command", "command": cmd}


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


def _read_unread_count(path: Path) -> int:
    """Read the unread message count, returning 0 on any error."""
    try:
        data = json.loads(path.read_text())
        count = data.get("count", 0)
        return int(count)
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        return 0


def _biff_segment(count: int) -> str:
    """Format the biff status segment.

    Plain ``biff(0)`` when no unreads; bold yellow ``biff(N)`` otherwise.
    """
    if count > 0:
        return f"\033[1;33mbiff({count})\033[0m"
    return "biff(0)"


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
