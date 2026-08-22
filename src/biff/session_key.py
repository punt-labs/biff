"""Session key resolution for PPID-keyed unread files.

Both the MCP server and the statusline command are descendants of the same
Claude Code process.  Walking up the process tree to the topmost ``claude``
ancestor gives both a stable key regardless of intermediate child processes.

See DESIGN.md DES-011a for the full rationale.
"""

from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)


def find_session_key() -> int:
    """Find the topmost ``claude`` ancestor PID as the session file key.

    Parses a single ``ps -eo pid=,ppid=,comm=`` call, then walks from the
    current process upward.  Returns the PID of the highest ancestor whose
    ``comm`` basename is ``claude``.

    Falls back to ``os.getppid()`` if ``ps`` fails or no ``claude`` ancestor
    is found (preserves pre-DES-011a behaviour).
    """
    cached = _get_cached()
    if cached is not None:
        return cached

    key = topmost_claude_pid()
    if key is None:
        key = os.getppid()
    _set_cached(key)
    return key


def topmost_claude_pid() -> int | None:
    """Return the topmost ``claude`` ancestor PID, or ``None`` if none.

    Distinct from :func:`find_session_key`: this returns ``None`` when no
    ``claude`` ancestor is in the process tree (or ``ps`` fails), rather
    than falling back to ``os.getppid()``.  A ``None`` result is the
    signal that the caller is *not* running under Claude Code — used by
    session-id routing to skip the hint lookup entirely in headless,
    CI, and SDK environments where no hint can exist.
    """
    try:
        table = _read_process_table()
    except (OSError, subprocess.SubprocessError):
        logger.warning(
            "Could not read the process table; treating as not under Claude Code",
            exc_info=True,
        )
        return None

    topmost_claude: int | None = None
    pid = os.getpid()
    for _ in range(10):  # safety bound — process trees are shallow
        entry = table.get(pid)
        if entry is None:
            break
        ppid, comm = entry
        if _is_claude(comm):
            topmost_claude = pid
        if ppid == pid or ppid == 0:
            break  # reached init / root
        pid = ppid
    return topmost_claude


def is_live_ancestor(pid: int) -> bool:
    """True when *pid* is currently one of this process's own ancestors.

    Re-derives the live ancestry chain from the OS on every call (the same
    ``ps`` parse :func:`topmost_claude_pid` uses), so a PID delivered via
    an env var captured once at process-spawn time (``CLAUDE_PID``) can be
    corroborated against the *actual, current* process tree rather than
    trusted on its bare existence. A long-lived process's cached env can
    outlive the ancestor it names -- if the OS later recycles that PID for
    an unrelated process, the env var still parses to a valid, currently
    running PID, but this walk will no longer find it among the caller's
    real ancestors, catching exactly that case.
    """
    try:
        table = _read_process_table()
    except (OSError, subprocess.SubprocessError):
        return False
    walk = os.getpid()
    for _ in range(10):  # safety bound — process trees are shallow
        if walk == pid:
            return True
        entry = table.get(walk)
        if entry is None:
            break
        ppid, _comm = entry
        if ppid == walk or ppid == 0:
            break  # reached init / root
        walk = ppid
    return False


# Cache (module-level, set once per process lifetime) -------------------------

_cached_key: int | None = None


def _get_cached() -> int | None:
    return _cached_key


def _set_cached(value: int) -> None:
    global _cached_key
    _cached_key = value


# Core algorithm --------------------------------------------------------------


def _read_process_table() -> dict[int, tuple[int, str]]:
    """Run ``ps`` and parse into ``{pid: (ppid, comm)}``."""
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,comm="],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        msg = f"ps exited with status {result.returncode}"
        raise subprocess.SubprocessError(msg)
    table: dict[int, tuple[int, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        table[pid] = (ppid, parts[2])
    return table


def _is_claude(comm: str) -> bool:
    """Check whether a ``comm`` value refers to a Claude Code process.

    ``ps`` on macOS reports either the full path
    (``/Applications/Claude.app/.../claude``) or just ``claude``.
    We match the basename.
    """
    basename = comm.rsplit("/", 1)[-1]
    return basename == "claude"
