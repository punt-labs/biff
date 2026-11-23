"""Reaping of shutdown sentinels left by sessions that exited."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from biff._stdlib import sentinel_dir
from biff.models import SessionEvent, UserSession
from biff.session_lock import routing_lock

if TYPE_CHECKING:
    from pathlib import Path

    from biff.server.state import ServerState

logger = logging.getLogger(__name__)


def build_logout_event(session_key: str, cached: UserSession) -> SessionEvent:
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


async def reap_sentinels(state: ServerState) -> None:
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
        if sentinel.is_file():
            await _reap_one(state, sentinel)


async def _reap_one(state: ServerState, sentinel: Path) -> None:
    """Reap the session one sentinel names, then remove the sentinel."""
    try:
        session_key = sentinel.read_text().strip()
    except OSError:
        return
    # Under identity routing a resumed session carries the SAME key as its
    # just-exited incarnation ({user}:{session_id} is stable across resume),
    # so a leftover sentinel for our own key must NOT be reaped — we are
    # already registered live under it, and reaping would log us out, release
    # the alias we just reclaimed, and delete our own KV row.  Consume it
    # without a logout, reservation release, or KV delete.
    if session_key == state.session_key:
        sentinel.unlink(missing_ok=True)
        return
    with routing_lock(session_key.partition(":")[2]) as acquired:
        if acquired and await _reap_dead_session(state, session_key):
            sentinel.unlink(missing_ok=True)


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
            await state.relay.append_wtmp(build_logout_event(session_key, session))
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
