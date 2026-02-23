"""Session key resolution for PPID-keyed unread files.

Both the MCP server and the statusline command are descendants of the same
Claude Code process.  Walking up the process tree to the topmost ``claude``
ancestor gives both a stable key regardless of intermediate child processes.

See DESIGN.md DES-011a for the full rationale.
"""

from __future__ import annotations

import os
import subprocess


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

    key = _walk_to_topmost_claude()
    _set_cached(key)
    return key


# Cache (module-level, set once per process lifetime) -------------------------

_cached_key: int | None = None


def _get_cached() -> int | None:
    return _cached_key


def _set_cached(value: int) -> None:
    global _cached_key
    _cached_key = value


# Core algorithm --------------------------------------------------------------


def _walk_to_topmost_claude() -> int:
    """Walk the process tree upward, return topmost ``claude`` PID.

    Falls back to ``os.getppid()`` when ``ps`` fails or no ``claude``
    ancestor exists.
    """
    fallback = os.getppid()
    try:
        table = _read_process_table()
    except (OSError, subprocess.SubprocessError):
        return fallback

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

    return topmost_claude if topmost_claude is not None else fallback


def _read_process_table() -> dict[int, tuple[int, str]]:
    """Run ``ps`` and parse into ``{pid: (ppid, comm)}``."""
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,comm="],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=5,
    )
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
