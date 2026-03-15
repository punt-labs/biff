"""Status line integration for Claude Code.

Provides install/uninstall for biff's status bar segment and the runtime
``biff statusline`` command that produces a complete, information-rich
status line: repo:branch, context usage, session cost, and biff messaging.

If the user had a pre-existing status line command before biff was installed,
it is stashed and its output replaces the repo/context/cost segments.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from biff.relay import atomic_write
from biff.session_key import find_session_key
from biff.unread import (
    DisplayItemView,
    SessionUnread,
    as_str_dict,
    read_session_unread,
)

# Well-known paths ----------------------------------------------------------

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
STASH_PATH = Path.home() / ".biff" / "statusline-original.json"
UNREAD_DIR = Path.home() / ".biff" / "unread"
SESSION_DATA_DIR = Path.home() / ".biff" / "session-data"

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

    Stashes the current ``statusLine`` value, replaces it with the
    ``biff statusline`` command.  The MCP server is registered via
    the plugin's ``mcpServers`` in ``plugin.json``, not here.
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

    Restores the original ``statusLine`` value from the stash and
    deletes the stash file.  The MCP server is managed by the plugin
    system, not here.
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


# Session data tee ----------------------------------------------------------


def _tee_session_data(
    raw: str,
    session_data_dir: Path = SESSION_DATA_DIR,
) -> None:
    """Persist raw session JSON for the lux applet. Never raises."""
    try:
        key = find_session_key()
        atomic_write(session_data_dir / f"{key}.json", raw)
    except (OSError, subprocess.SubprocessError):
        pass


# Runtime -------------------------------------------------------------------


def run_statusline(
    stash_path: Path = STASH_PATH,
    unread_dir: Path = UNREAD_DIR,
) -> str:
    """Produce the status bar text for Claude Code.

    Line 1: ``repo:branch | ctx% | $cost | biff``
    Line 2: current display queue item — wall (bold red) or talk
    (bold yellow), falling back to an idle marker when the queue is empty.

    If a stashed original command exists and succeeds, its output replaces
    the repo/context/cost segments (the user chose their own base).  The
    biff messaging segment is always appended.
    """
    stdin_data = sys.stdin.read()
    _tee_session_data(stdin_data)
    session = _parse_session_data(stdin_data)

    original_cmd = _resolve_original_command(stash_path)
    original_output = _run_original(original_cmd, stdin_data) if original_cmd else None

    if original_output is not None:
        base_segments = [original_output]
    else:
        base_segments = _base_segments(session)

    unread = read_session_unread(unread_dir / f"{find_session_key()}.json")
    biff = _biff_segment(unread)
    display = _display_segment(unread.display_items if unread else ())

    segments = [s for s in [*base_segments, biff] if s.strip()]
    line1 = " | ".join(segments)
    line2 = display if display.strip() else _LINE2_IDLE
    return f"{line1}\n{line2}"


# Session data parsing ------------------------------------------------------


def _parse_session_data(stdin_data: str) -> dict[str, object]:
    """Parse the JSON session blob from Claude Code's stdin."""
    try:
        return as_str_dict(json.loads(stdin_data))
    except (json.JSONDecodeError, ValueError):
        return {}


# Base segments (repo, context, cost) ---------------------------------------


def _base_segments(session: dict[str, object]) -> list[str]:
    """Build the non-biff segments: repo:branch, context%, $cost."""
    segments: list[str] = []
    git = _git_segment(session)
    if git:
        segments.append(git)
    ctx = _context_segment(session)
    if ctx:
        segments.append(ctx)
    cost = _cost_segment(session)
    if cost:
        segments.append(cost)
    return segments


def _git_segment(session: dict[str, object]) -> str:
    """Format ``repo:branch`` from workspace path and git.

    Claude Code sends ``workspace`` as an object with ``project_dir``
    and ``current_dir`` keys (not a plain string).
    """
    workspace_raw = session.get("workspace")
    ws = as_str_dict(workspace_raw)
    if ws:
        workspace_dir = ws.get("project_dir") or ws.get("current_dir", "")
    elif isinstance(workspace_raw, str):
        workspace_dir = workspace_raw
    else:
        return ""
    if not isinstance(workspace_dir, str) or not workspace_dir:
        return ""
    repo_name = Path(workspace_dir).name
    branch = _git_branch(workspace_dir)
    if branch:
        return f"{repo_name}:{branch}"
    return repo_name


def _git_branch(workspace: str) -> str:
    """Get the current git branch name, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=2,
            cwd=workspace,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return ""


def _context_segment(session: dict[str, object]) -> str:
    """Format context window usage as a color-coded percentage.

    Uses ``used_percentage`` from Claude Code when available (preferred),
    falling back to manual calculation from token counts.

    Green (default) below 50%, yellow 50-79%, red at 80%+.
    """
    try:
        cw = as_str_dict(session.get("context_window"))
        pct_raw = cw.get("used_percentage")
        if isinstance(pct_raw, (int, float)):
            pct = int(pct_raw)
        else:
            usage = as_str_dict(cw.get("current_usage"))
            size = cw.get("context_window_size", 0)
            if not isinstance(size, (int, float)) or size <= 0:
                return ""
            current = (
                _int_field(usage, "input_tokens")
                + _int_field(usage, "cache_creation_input_tokens")
                + _int_field(usage, "cache_read_input_tokens")
            )
            pct = int(current * 100 / size)
        label = f"{pct}%"
        if pct >= 80:
            return f"\033[31m{label}\033[0m"
        if pct >= 50:
            return f"\033[33m{label}\033[0m"
        return label
    except (TypeError, ValueError, ZeroDivisionError):
        return ""


def _cost_segment(session: dict[str, object]) -> str:
    """Format the session cost as ``$X.XX``."""
    try:
        cost = as_str_dict(session.get("cost"))
        total = cost.get("total_cost_usd", 0)
        if isinstance(total, (int, float)) and total > 0:
            return f"${total:.2f}"
    except (TypeError, ValueError):
        pass
    return ""


def _int_field(data: dict[str, object], key: str) -> int:
    """Extract an integer field from a dict, defaulting to 0."""
    val = data.get(key, 0)
    return int(val) if isinstance(val, (int, float)) else 0


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


def _biff_segment(unread: SessionUnread | None) -> str:
    """Format the biff status segment for a single session.

    No file → dim enable hint (``/biff y to enable team communication``).
    Mesg off → ``user:tty(n)`` plain (regardless of actual count).
    Zero count → ``user:tty(0)`` plain.
    Nonzero → ``user:tty(N)`` bold yellow.
    """
    if unread is None:
        return "\033[2m/biff y to enable team communication\033[0m"
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


_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;]*[A-Za-z]|\][^\x07]*\x07?|[()][A-B012])")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Line 2 is always emitted to prevent the status bar from jumping between
# 1 and 2 lines.  The ▶ marker keeps the line non-empty so Claude Code's
# renderer allocates the row, and is visually consistent with biff's other
# command outputs (/who, /finger, /read, /last).
_LINE2_IDLE = "▶"


_TURN_DURATION = 15  # seconds per item in rotation


def _display_segment(items: tuple[DisplayItemView, ...]) -> str:
    """Pick the current display item via time-based rotation.

    Uses ``int(time.time() / _TURN_DURATION) % len(items)`` so every
    invocation within the same 15-second window shows the same item,
    and rotation advances deterministically without any persisted state.

    Wall items are bold red, talk items are bold yellow.  Both use the
    ``▶`` prefix for visual consistency across idle and active states.
    Sanitizes ANSI escape sequences and control characters but does not
    truncate — Claude Code's renderer manages line width.
    """
    if not items:
        return ""
    import time  # noqa: PLC0415

    index = int(time.time() / _TURN_DURATION) % len(items)
    item = items[index]
    if not item.text:
        return ""
    clean = _ANSI_RE.sub("", item.text)
    clean = _CTRL_RE.sub("", clean)
    clean = " ".join(clean.split())
    if not clean:
        return ""
    if item.kind == "wall":
        return f"▶ \033[1;31m{clean}\033[0m"
    return f"▶ \033[1;33m{clean}\033[0m"


def _run_original(command: str, stdin_data: str) -> str | None:
    """Run the original status line command, returning its stdout.

    Returns ``None`` on failure (timeout, bad exit, etc.) so callers can
    distinguish "command failed" from "command succeeded with empty output".
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
            return None
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return None
