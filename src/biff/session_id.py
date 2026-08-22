"""Claude session-id routing identity — the durable, non-recycled anchor.

Biff routes on the Claude Code ``session_id``, which is stable across
``claude --resume``/``--continue`` and fresh on ``--fork-session``.

Claude Code sets ``CLAUDE_PID`` directly in the environment of every
subprocess it spawns — hooks and the MCP server alike — as of Claude Code
2.1.234 (DES-011's original claim that no session-scoped channel existed
is stale). It names the *owning claude process's own PID*, delivered
directly rather than inferred by walking the process tree, and — unlike
that walk — it is distinct per nested claude process rather than
collapsing every nesting level onto one shared "topmost ancestor" PID.
:func:`_resolve_claude_pid` and :meth:`SessionHint.resolve_routing_id`
both prefer it, falling back to the process-tree walk only for older
Claude Code versions or a genuinely headless/CI/SDK context where the
env var is absent.

A ``CLAUDE_CODE_SESSION_ID`` env var also exists but is *not* read here:
it is frozen at subprocess-spawn time, so the long-lived MCP server's own
copy goes stale the moment ``/clear`` (or any other in-place session-id
change) fires — the hook subprocess sees the fresh value on its next
invocation, but the server does not, silently reopening the same
misrouting this module exists to prevent. ``CLAUDE_PID`` has no such
problem: it names the owning *process*, which does not change on
``/clear``, only the session_id within it does — and the hint file below
is rewritten fresh on every SessionStart, including a ``/clear``-sourced
one, so reading it (rather than caching the raw session_id) stays live
across the process's whole lifetime.

A SessionStart hook writes a :class:`SessionHint` to
``~/.punt-labs/biff/sessions/{claude_pid}.json``, keyed by
``CLAUDE_PID`` — never by the process-tree walk's topmost ancestor,
which previously collapsed a nested claude process onto its parent's
key and let the nested session's hint overwrite the parent's. The
server reads the hint back from the same key. The value becomes the
routing token carried in the session key (``{user}:{session_id}``),
replacing the volatile random hex.

The recycle guard is ``(pid, process-start-time)``: a leftover hint from a
dead session whose PID was later reused by a *different* claude is rejected
because the process start times differ.  There is no time-based freshness
window — ``session_id`` is Claude-delivered on every resume, so a long gap is
a normal resume, not a stale hint.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from hashlib import blake2b
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast

import psutil

from biff._stdlib import biff_data_dir
from biff.session_key import is_live_ancestor, topmost_claude_pid
from biff.tty import validate_routing_id

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

_DERIVE_DIGEST_BYTES = 8  # 16 hex chars — inside the routing-id charset
_RESOLVE_ATTEMPTS = 10
_RESOLVE_DELAY_S = 0.05  # SessionStart fires before MCP connect; retry is a safety net


@dataclass(frozen=True, slots=True)
class SessionHint:
    """A SessionStart hook's record of the Claude session for one claude PID.

    The routing identity (``session_id``) is Claude-delivered per boot and
    persisted only for the life of the ``claude`` process.  The
    ``claude_start_time`` is the recycle guard — see the module docstring.
    """

    session_id: str
    claude_pid: int
    claude_start_time: float
    source: str

    @classmethod
    def capture(cls, session_id: str, source: str) -> Self:
        """Build a hint keyed on this process's owning ``claude`` PID.

        Called from the SessionStart hook, a descendant of the ``claude``
        process it must key on; see :func:`_resolve_claude_pid` for how
        that PID is found. The start time is best-effort: if it cannot be
        read the hint is still written with ``0.0``, which simply forfeits
        reclaim (the guard rejects it) rather than misrouting.
        """
        pid = _resolve_claude_pid()
        return cls(
            session_id=session_id,
            claude_pid=pid,
            claude_start_time=_process_start_time(pid),
            source=source,
        )

    @classmethod
    def load(cls, pid: int) -> Self | None:
        """Read the hint for *pid*, or ``None`` if absent or malformed.

        Logging is symmetric so "no hint" (a readable absence, at DEBUG) is
        distinguishable from "hint exists but unusable" (malformed JSON,
        wrong top-level type, or wrong shape — each at WARNING).
        """
        path = _hint_path(pid)
        try:
            raw = path.read_text()
        except OSError:
            logger.debug("No readable session hint at %s", path)
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Malformed session hint at %s", path)
            return None
        if not isinstance(data, dict):
            logger.warning("Session hint at %s is not a JSON object", path)
            return None
        hint = cls._from_mapping(cast("dict[str, object]", data))
        if hint is None:
            logger.warning("Session hint at %s has an unexpected shape", path)
        return hint

    @classmethod
    def resolve_routing_id(cls) -> str | None:
        """Return the routing ``session_id`` for this server, or ``None``.

        Keys the hint-file lookup on ``CLAUDE_PID`` when present —
        corroborated against this process's *current, live* ancestry via
        :func:`biff.session_key.is_live_ancestor` before it is trusted.
        ``CLAUDE_PID`` is captured once, at this process's own spawn time,
        and never re-observed after that; a long-lived server holding a
        stale value whose real ancestor has since died and had its PID
        recycled by an unrelated, legitimate claude session would
        otherwise pass the recycle guard below too — that guard only
        checks "does *someone's* start time at this PID match the hint,"
        which a fresh, self-consistent hint from the recycling session
        satisfies trivially. Live-ancestry corroboration is the check that
        actually answers "is this PID still *my* ancestor." Falls back to
        the topmost-ancestor process-tree walk when the env var is absent
        or fails corroboration — older Claude Code versions, a genuinely
        headless/CI/SDK context, or exactly the stale-PID scenario above.

        Reads the hint that the SessionStart hook left for the resolved
        PID, and validates the recycle guard and token shape.  Returns
        ``None`` — the caller then mints a fresh hex via
        :func:`biff.tty.generate_tty` — when there is no ``claude``
        ancestor (headless/CI/SDK), no hint, a recycled-PID mismatch, or a
        malformed token.  A short bounded retry covers the rare case where
        the server reads before the hook has finished writing.
        """
        pid = _claude_pid_from_env()
        pid_source = "CLAUDE_PID"
        if pid is not None and not is_live_ancestor(pid):
            logger.warning(
                "CLAUDE_PID=%d is not among this process's live ancestors "
                "(stale env, real ancestor likely died and its PID was "
                "recycled); falling back to the process-tree walk",
                pid,
            )
            pid = None
        if pid is None:
            pid = topmost_claude_pid()
            pid_source = "process-tree walk"
        if pid is None:
            return None  # not under Claude Code — headless/CI/SDK, no warning
        for attempt in range(_RESOLVE_ATTEMPTS):
            hint = cls.load(pid)
            if hint is not None:
                routing_id = hint._validated_routing_id(pid)
                if routing_id is not None:
                    logger.debug("Routing id resolved via %s (pid %d)", pid_source, pid)
                    return routing_id
                break  # hint present but invalid — retrying cannot help
            if attempt < _RESOLVE_ATTEMPTS - 1:
                time.sleep(_RESOLVE_DELAY_S)
        logger.warning(
            "under Claude Code (pid %d, via %s) but no valid session hint; "
            "routing on a volatile id — resume-reclaim disabled",
            pid,
            pid_source,
        )
        return None

    @staticmethod
    def derive_routing_id(session_id: str, role: str) -> str:
        """Return a stable hex routing id for *role* within *session_id*.

        A deterministic pairing of ``(session_id, role)`` (the spec's
        ``derive``): stable across resume and distinct per role by
        construction.  The digest is hex (16 chars), inside the
        routing-token charset ``[0-9a-fA-F-]``, so a ``{session_id}:{role}``
        composite — whose ``:`` would collide with the session-key
        separator — is never used.  The companion (human) session derives
        its id this way so it is not volatile.
        """
        digest = blake2b(
            f"{session_id}:{role}".encode(), digest_size=_DERIVE_DIGEST_BYTES
        )
        return digest.hexdigest()

    def write(self) -> None:
        """Persist this hint to ``sessions/{claude_pid}.json`` (best-effort)."""
        self._write_to(_hint_path(self.claude_pid))

    def _write_to(self, path: Path) -> None:
        """Atomically write this hint's JSON payload to *path* (best-effort).

        The durable routing id is owner-private: the directory is chmod'd
        ``0o700`` and the file is created ``0o600`` from its first byte, so a
        routing id is never world-readable on a multi-user host — not even
        transiently.  The temp file is opened with ``O_CREAT|O_WRONLY|O_TRUNC``
        at mode ``0o600`` (a plain ``write_text`` would create it ``0o644``
        under the default umask) and ``O_NOFOLLOW`` refuses a symlink at the
        path.  The atomic temp-then-replace is preserved.
        """
        sessions_dir = path.parent
        sessions_dir.mkdir(parents=True, exist_ok=True)
        sessions_dir.chmod(0o700)  # umask-independent
        payload = json.dumps(
            {
                "session_id": self.session_id,
                "claude_pid": self.claude_pid,
                "claude_start_time": self.claude_start_time,
                "source": self.source,
            }
        )
        tmp = path.with_suffix(".json.tmp")
        flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp, flags, 0o600)
        try:
            os.write(fd, payload.encode())
        finally:
            os.close(fd)
        tmp.chmod(0o600)  # umask-independent, in case the create mode was masked
        tmp.replace(path)

    def matches_running(self) -> bool:
        """True when the live ``claude_pid`` still has this start time.

        The recycle guard: a hint left by a dead session whose PID was
        reused by a different process has a different start time and is
        rejected.  ``0.0`` means "unknown" — an unreadable live process *or*
        a capture-time psutil fault — and never matches, so an unknown start
        time can never bind a stale id to a recycled PID.
        """
        live = _process_start_time(self.claude_pid)
        return live != 0.0 and live == self.claude_start_time

    def _validated_routing_id(self, pid: int) -> str | None:
        """Return ``session_id`` if the hint is bound to the live *pid*."""
        if self.claude_pid != pid:
            logger.warning(
                "Session hint pid %d does not match walked pid %d; "
                "ignoring stale leftover",
                self.claude_pid,
                pid,
            )
            return None  # hint filename/PID cross-check (stale leftover)
        if not self.matches_running():
            logger.info("Session hint for pid %d failed recycle guard", pid)
            return None
        if validate_routing_id(self.session_id) is not None:
            logger.warning("Ignoring malformed session_id in hint for pid %d", pid)
            return None
        return self.session_id

    @classmethod
    def _from_mapping(cls, data: Mapping[str, object]) -> Self | None:
        """Build a hint from parsed JSON, or ``None`` on a shape mismatch."""
        session_id = data.get("session_id")
        claude_pid = data.get("claude_pid")
        start_time = data.get("claude_start_time")
        source = data.get("source", "")
        if (
            not isinstance(session_id, str)
            or not isinstance(claude_pid, int)
            or not isinstance(start_time, int | float)
            or not isinstance(source, str)
        ):
            return None
        return cls(
            session_id=session_id,
            claude_pid=claude_pid,
            claude_start_time=float(start_time),
            source=source,
        )


def _sessions_dir() -> Path:
    """Directory of per-claude-PID session hints."""
    return biff_data_dir() / "sessions"


def _hint_path(pid: int) -> Path:
    """Hint file path for a ``claude`` PID."""
    return _sessions_dir() / f"{pid}.json"


def _claude_pid_from_env() -> int | None:
    """Return the owning claude process's PID from ``CLAUDE_PID``, or ``None``.

    Delivered directly by Claude Code on every hook and MCP-server
    subprocess (2.1.234+) — distinct per nested claude process, unlike
    the topmost-ancestor walk, which deliberately collapses every nesting
    level onto one shared PID.  ``None`` when absent (older Claude Code)
    or unparseable, never a wrong PID silently used.
    """
    raw = os.environ.get("CLAUDE_PID")
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        logger.warning("CLAUDE_PID=%r is not a valid integer; ignoring", raw)
        return None
    if pid <= 0:
        # int() accepts "-1"/"+0"; psutil.Process(pid<=0) raises ValueError,
        # which _process_start_time's except (psutil.Error, OSError) does
        # NOT catch -- an uncaught crash in the SessionStart hook, not a
        # clean fallback. No real process ever reports a PID <= 0.
        logger.warning("CLAUDE_PID=%r is not a valid pid; ignoring", raw)
        return None
    return pid


def _resolve_claude_pid() -> int:
    """Return this process's owning claude PID: env first, then the walk.

    Prefers ``CLAUDE_PID`` (see :func:`_claude_pid_from_env`). Falls back
    to :func:`topmost_claude_pid`, and finally to the parent PID so a hint
    is still written under *some* stable key when both are unavailable —
    the server's recycle guard rejects a mismatch, so a wrong key only
    forfeits reclaim. Every step is an explicit ``is not None`` check, not
    an ``or`` chain: ``_claude_pid_from_env`` and ``topmost_claude_pid``
    are already guaranteed non-zero/positive by their own validation, but
    an ``or`` chain would silently misinterpret a legitimate falsy int
    (were one ever possible) as absence — cheap to rule out entirely
    rather than rely on that guarantee holding forever.
    """
    pid = _claude_pid_from_env()
    if pid is not None:
        return pid
    pid = topmost_claude_pid()
    if pid is not None:
        return pid
    return os.getppid()


def _process_start_time(pid: int) -> float:
    """Return the process creation time of *pid*, or ``0.0`` if unreadable.

    Uses ``psutil.Process.create_time()`` — uniform across Linux, macOS, and
    Windows Unix-emulation (WSL/Cygwin/Git-Bash) — so no per-OS ``/proc`` vs
    ``ps`` parsing is needed.  ``0.0`` (a value no real process reports)
    means "unknown", which fails the recycle guard.
    """
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return 0.0
