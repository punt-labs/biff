"""Run the poller and the sentinel reaping it does; restart it when it dies."""

from __future__ import annotations

import asyncio
import logging
from contextvars import Context
from typing import TYPE_CHECKING, Protocol, Self

from biff._stdlib import sentinel_dir
from biff.models import SessionEvent, UserSession
from biff.server.tools import _descriptions
from biff.session_lock import routing_lock

if TYPE_CHECKING:
    from pathlib import Path

    from fastmcp import FastMCP

    from biff.server.state import ServerState

logger = logging.getLogger(__name__)

_RESTART_DELAY = 1.0  # seconds, doubled per consecutive death, capped
_RESTART_CAP = 60.0
_DEGRADED_AFTER = 5  # consecutive deaths before the user is told


class LiveTasks(Protocol):
    """What the supervisor needs of the session that owns the poller."""

    shutdown: asyncio.Event
    reaper: asyncio.Task[None] | None
    tasks: list[asyncio.Task[None]]


class PollerSupervisor:
    """Start the poller; if it dies, log it and start another after a backoff.

    The poller also reaps shutdown sentinels: it runs on every wake, and a
    watch on the sentinel directory wakes it, so there is no reaper timer.
    It outlives the requests that may start it, and nothing else would
    restart it, so a death must not leave the markers silently stale.

    The backoff grows with the deaths in a row (``state.wake.health``), which end
    only when a poller completes a pass with a liveness check.  The first death
    and the ``_DEGRADED_AFTER``-th get a traceback; from the latter on, and
    whenever a restart itself raises, the ``read_messages`` description tells
    the user that notifications stopped; the rest are one-line warnings.
    """

    __slots__ = ("_live", "_mcp", "_shutdown", "_state")

    _live: LiveTasks
    _mcp: FastMCP[ServerState]
    _state: ServerState
    _shutdown: asyncio.Event

    def __new__(
        cls, live: LiveTasks, mcp: FastMCP[ServerState], state: ServerState
    ) -> Self:
        """Supervise the poller of *live*'s current session."""
        self = super().__new__(cls)
        self._live, self._mcp, self._state = live, mcp, state
        self._shutdown = live.shutdown
        return self

    @staticmethod
    def restart_delay(attempt: int) -> float:
        """Return the backoff before restart number *attempt* (capped, no overflow)."""
        return min(_RESTART_DELAY * (1 << min(attempt, 16)), _RESTART_CAP)

    def start(self) -> None:
        """Start a poller and watch it."""
        state = self._state
        poll = _descriptions.poll_inbox(
            self._mcp,
            state,
            shutdown=self._shutdown,
            reap=lambda: SentinelReaper.reap(state),
            watch_dirs=(sentinel_dir(state.config.repo_name),),
        )
        task = asyncio.create_task(poll, context=Context())
        self._live.reaper = task
        self._live.tasks.append(task)
        task.add_done_callback(self._died)

    def _died(self, done: asyncio.Task[None]) -> None:
        exc = None if done.cancelled() else done.exception()
        if exc is not None and not self._shutdown.is_set():
            self._failed(exc, degrade=False)

    def _failed(self, exc: BaseException, *, degrade: bool) -> None:
        """Record a death (or a failed restart), tell the user, retry later."""
        deaths = self._state.wake.health.died()
        if deaths in (1, _DEGRADED_AFTER):
            logger.error("Poller task died (%d in a row)", deaths, exc_info=exc)
        elif deaths < _DEGRADED_AFTER or deaths % 10 == 0:
            logger.warning("Poller task died again (%d in a row): %s", deaths, exc)
        if degrade or deaths >= _DEGRADED_AFTER:
            self._state.wake.health.degrade()
            self._live.tasks.append(asyncio.create_task(self._show_notice()))
        asyncio.get_running_loop().call_later(
            self.restart_delay(deaths - 1), self._restart
        )

    async def _show_notice(self) -> None:
        """Render the notice into the description (a refresh shows it)."""
        await _descriptions.refresh_read_messages(self._mcp, self._state)

    def _restart(self) -> None:
        """Start another poller unless cleanup began; a start that raises is retried."""
        if self._shutdown.is_set() or self._live.shutdown is not self._shutdown:
            return  # cleanup began, shut down, or re-pointed to a new session
        try:
            self.start()
        except Exception as exc:
            logger.exception("Poller restart failed")
            self._failed(exc, degrade=True)


class SentinelReaper:
    """Reaping of the shutdown sentinels that exited sessions left behind."""

    @staticmethod
    def logout_event(session_key: str, cached: UserSession) -> SessionEvent:
        """Build a logout ``SessionEvent`` from a cached session."""
        return SessionEvent(
            session_key=session_key,
            event="logout",
            user=cached.user,
            tty=cached.tty,
            tty_name=cached.tty_name,
            hostname=cached.hostname,
            pwd=cached.pwd,
            timestamp=cached.last_active,
            plan=cached.plan,
            repo=cached.repo,
        )

    @staticmethod
    async def reap(state: ServerState) -> None:
        """Process sentinel files, writing logout events and deleting sessions.

        When a biff server receives SIGTERM/SIGINT, the signal handler writes
        a sentinel file containing the session key.  This function processes
        those sentinels on startup and whenever the poller wakes (a watch on the
        sentinel directory wakes it when one appears):

        1. Fetch the session from KV (still present — 3-day TTL)
        2. Append a wtmp logout event with ``last_active`` as the timestamp
        3. Delete the KV entry
        4. Remove the sentinel file

        This is the primary logout mechanism for the common case (graceful
        shutdown via signal).  The orphan detector handles the fallback case
        (SIGKILL, OOM, power loss — no sentinel written).
        """
        d = sentinel_dir(state.config.repo_name)
        for sentinel in d.iterdir() if d.exists() else ():
            if sentinel.is_file() and not sentinel.name.startswith("."):
                await SentinelReaper._reap_one(state, sentinel)

    @staticmethod
    async def _reap_one(state: ServerState, sentinel: Path) -> None:
        """Reap the session one sentinel names, then remove the sentinel."""
        try:
            session_key = sentinel.read_text().strip()
        except OSError:
            return
        user, _, session_id = session_key.partition(":")
        if not (user and session_id):
            return  # not a sentinel: never ask the relay about ""
        # Under identity routing a resumed session carries the SAME key as its
        # just-exited incarnation ({user}:{session_id} is stable across resume),
        # so a leftover sentinel for our own key must NOT be reaped — we are
        # already registered live under it, and reaping would log us out, release
        # the alias we just reclaimed, and delete our own KV row.  Consume it
        # without a logout, reservation release, or KV delete.
        if session_key == state.session_key:
            sentinel.unlink(missing_ok=True)
            return
        with routing_lock(session_id) as acquired:
            if acquired and await SentinelReaper._reap_dead_session(state, session_key):
                sentinel.unlink(missing_ok=True)

    @staticmethod
    async def _reap_dead_session(state: ServerState, session_key: str) -> bool:
        """Log out, release the alias, and delete the KV row for a dead session.

        Returns ``True`` when the KV row was deleted (the caller should remove the
        sentinel), ``False`` when the delete failed (keep the sentinel to retry).
        """
        # Write logout event before deleting the session.  The KV entry is still
        # present (3-day TTL), so we can fetch session data for an accurate
        # last-seen timestamp.
        session: UserSession | None = None
        try:
            session = await state.relay.get_session(session_key)
            if session is not None:
                await state.relay.append_wtmp(
                    SentinelReaper.logout_event(session_key, session)
                )
        except Exception:  # noqa: BLE001
            logger.warning("Failed to write sentinel logout")
        # Release TTY name reservation before deleting session (DES-035).
        # Separate try block so a wtmp failure doesn't leak the name reservation.
        if session is not None and session.tty_name:
            try:
                await state.relay.release_tty_name(session.user, session.tty_name)
            except Exception:
                logger.warning(
                    "Failed to release TTY name %s during sentinel reap",
                    session.tty_name,
                    exc_info=True,
                )
        try:
            await state.relay.delete_session(session_key)
        except Exception:
            logger.warning("Failed to reap sentinel", exc_info=True)
            return False
        return True
