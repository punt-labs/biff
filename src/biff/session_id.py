"""Claude session-id routing identity — the durable, non-recycled anchor.

Biff routes on the Claude Code ``session_id``, which is stable across
``claude --resume``/``--continue`` and fresh on ``--fork-session`` (biff-7ak).
The MCP server never observes ``session_id`` directly (DES-011): only the
SessionStart hook sees it, on stdin.  This module is the bridge.

A SessionStart hook writes a :class:`SessionHint` to
``~/.punt-labs/biff/sessions/{claude_pid}.json``.  The server, a descendant
of the same ``claude`` process, walks the process tree to that PID and reads
the hint back.  The value becomes the routing token carried in the session
key (``{user}:{session_id}``), replacing the volatile random hex.

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
import re
import time
from dataclasses import dataclass
from hashlib import blake2b
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast

import psutil

from biff._stdlib import biff_data_dir
from biff.session_key import topmost_claude_pid
from biff.tty import validate_routing_id

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

_DERIVE_DIGEST_BYTES = 8  # 16 hex chars — inside the routing-id charset
_RESOLVE_ATTEMPTS = 10
_RESOLVE_DELAY_S = 0.05  # SessionStart fires before MCP connect; retry is a safety net

# Safe charset for an ``agent_id`` used as a filesystem path component.
# Same semantics as ``markers._identity_component``: a superset of the routing
# id charset that still refuses anything a path could traverse (``/``, ``..``,
# ``\``, leading ``.``-only). Untrusted values -- ``agent_id`` arrives on the
# SubagentStart hook stdin -- MUST match before composition; ``../{pid}`` or
# ``/tmp/x`` otherwise resolve out of the sessions directory (see
# :func:`_agent_hint_path`).
_SAFE_AGENT_ID_RE = re.compile(r"^[0-9a-zA-Z_-]{1,128}$")


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
        """Build a hint for the current process's topmost ``claude`` ancestor.

        Called from the SessionStart hook, which is a descendant of the
        ``claude`` process it must key on.  The start time is best-effort:
        if it cannot be read the hint is still written with ``0.0``, which
        simply forfeits reclaim (the guard rejects it) rather than
        misrouting.
        """
        pid = _resolve_claude_pid()
        return cls(
            session_id=session_id,
            claude_pid=pid,
            claude_start_time=_process_start_time(pid),
            source=source,
        )

    @classmethod
    def capture_for_agent(cls, agent_id: str, source: str) -> Self:
        """Build a hint for an in-process subagent's own ``agent_id``.

        Same recycle-guard shape as :meth:`capture`, but the identity
        being recorded is the subagent's own ``agent_id`` rather than a
        top-level session's ``session_id`` — the two are written to
        different files (:meth:`write` vs :meth:`write_agent_hint`) so a
        subagent's hint is never confused with, or overwrites, its
        leader's own per-pid hint (biff-ar1/om9 design §3b).
        """
        pid = _resolve_claude_pid()
        return cls(
            session_id=agent_id,
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

        Walks to the topmost ``claude`` ancestor, reads the hint that the
        SessionStart hook left for it, and validates the recycle guard and
        token shape.  Returns ``None`` — the caller then mints a fresh hex
        via :func:`biff.tty.generate_tty` — when there is no ``claude``
        ancestor (headless/CI/SDK), no hint, a recycled-PID mismatch, or a
        malformed token.  A short bounded retry covers the rare case where
        the server reads before the hook has finished writing.
        """
        pid = topmost_claude_pid()
        if pid is None:
            return None  # not under Claude Code — headless/CI/SDK, no warning
        for attempt in range(_RESOLVE_ATTEMPTS):
            hint = cls.load(pid)
            if hint is not None:
                routing_id = hint._validated_routing_id(pid)
                if routing_id is not None:
                    return routing_id
                break  # hint present but invalid — retrying cannot help
            if attempt < _RESOLVE_ATTEMPTS - 1:
                time.sleep(_RESOLVE_DELAY_S)
        logger.warning(
            "under Claude Code (pid %d) but no valid session hint; routing on "
            "a volatile id — resume-reclaim disabled",
            pid,
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
        its id this way so it is not volatile (biff-7ak amendment 3).
        """
        digest = blake2b(
            f"{session_id}:{role}".encode(), digest_size=_DERIVE_DIGEST_BYTES
        )
        return digest.hexdigest()

    def write(self) -> None:
        """Persist this hint to ``sessions/{claude_pid}.json`` (best-effort)."""
        self._write_to(_hint_path(self.claude_pid))

    def write_agent_hint(self, agent_id: str) -> None:
        """Persist this hint to ``sessions/{claude_pid}/{agent_id}.json``.

        Same write semantics as :meth:`write`, but keyed additionally by
        *agent_id* under the shared leader ``claude_pid`` rather than the
        leader's own per-pid file — so an in-process subagent's identity
        is discoverable without ever overwriting its leader's hint
        (biff-ar1/om9 design §3b).

        Raises ``ValueError`` if *agent_id* would escape the intended
        ``sessions/{claude_pid}/`` directory: :func:`_agent_hint_path`
        rejects a bad charset, and the resolved-parent containment check
        below refuses any residual traversal BEFORE ``_write_to``'s
        ``mkdir``/``chmod`` can amplify it onto an unintended directory.
        """
        path = _agent_hint_path(self.claude_pid, agent_id)
        _require_agent_hint_contained(path, self.claude_pid)
        self._write_to(path)

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


def is_safe_agent_id(agent_id: str) -> bool:
    """Return ``True`` when *agent_id* is safe as a filesystem path component.

    The predicate the trust boundary (``hook._capture_subagent_hint``)
    calls before composing an ``agent_id`` from an untrusted hook
    payload into a path. Matches :data:`_SAFE_AGENT_ID_RE`; a value that
    fails must be dropped (fail closed) rather than sanitized, because
    :class:`pathlib.Path`'s ``/`` operator honors ``..`` and absolute
    components — silent coercion would still write to an unintended
    directory.
    """
    return bool(_SAFE_AGENT_ID_RE.match(agent_id))


def _agent_hint_path(pid: int, agent_id: str) -> Path:
    """Hint file path for one subagent's own identity under a ``claude`` PID.

    A subdirectory per PID, not a suffix on the leader's own filename, so
    it can never collide with :func:`_hint_path`'s ``{pid}.json``.

    Validates *agent_id* against :func:`is_safe_agent_id` before
    composition: an unsanitized value from the SubagentStart hook
    payload could otherwise escape the sessions directory
    (``agent_id="../{pid}"`` resolves to and overwrites the leader
    hint; ``agent_id="/tmp/x"`` composes to an absolute path since
    :class:`pathlib.Path`'s ``/`` honors absolute components). Callers
    should sanitize at the trust boundary; this raise is
    defense-in-depth so a future caller cannot re-open the hole.
    """
    if not is_safe_agent_id(agent_id):
        msg = f"unsafe agent id: {agent_id!r}"
        raise ValueError(msg)
    return _sessions_dir() / str(pid) / f"{agent_id}.json"


def _require_agent_hint_contained(path: Path, claude_pid: int) -> None:
    """Refuse *path* if it resolves outside ``sessions/{claude_pid}/``.

    Last-line-of-defense containment check: even if some future caller
    composes an unsafe component into *path*, a resolved parent outside
    the intended directory refuses the write BEFORE
    :meth:`SessionHint._write_to`'s ``mkdir``/``chmod`` can amplify the
    traversal by tightening permissions on an unintended directory.
    """
    allowed = (_sessions_dir() / str(claude_pid)).resolve(strict=False)
    resolved = path.resolve(strict=False)
    if resolved.parent != allowed:
        msg = f"agent hint path {resolved} escapes {allowed}"
        raise ValueError(msg)


def _resolve_claude_pid() -> int:
    """Return the topmost ``claude`` PID for the hook's own process tree.

    The hook runs under ``claude``; :func:`topmost_claude_pid` returns that
    PID.  When it cannot (``ps`` failure), fall back to the parent PID so a
    hint is still written under *some* stable key — the server's recycle
    guard rejects a mismatch, so a wrong key only forfeits reclaim.
    """
    return topmost_claude_pid() or os.getppid()


def _process_start_time(pid: int) -> float:
    """Return the process creation time of *pid*, or ``0.0`` if unreadable.

    Uses ``psutil.Process.create_time()`` — uniform across Linux, macOS, and
    Windows Unix-emulation (WSL/Cygwin/Git-Bash) — so no per-OS ``/proc`` vs
    ``ps`` parsing is needed (biff-7ak amendment 4a).  ``0.0`` (a value no
    real process reports) means "unknown", which fails the recycle guard.
    """
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return 0.0
