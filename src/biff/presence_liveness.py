"""Which sessions are live, and which died without deregistering."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from biff.models import UserSession
from biff.relay import PRESENCE_LIVENESS_SECONDS

# Threshold for reporting a session as "stopped responding" in the /who
# footnote (DES-057).  Wider than PRESENCE_LIVENESS_SECONDS on purpose: a
# session that misses a single heartbeat tick (laptop sleep, GC pause)
# already drops out of live_sessions() for that one poll and self-heals on
# the next tick.  Reporting it as dead in the same breath would flap it
# into and back out of the footnote.  3x the liveness window tolerates
# several consecutive missed ticks (~4 missed heartbeats past the liveness
# cutoff) before treating the row as a genuine orphan.
DEAD_REPORT_SECONDS = PRESENCE_LIVENESS_SECONDS * 3


def live_sessions(sessions: Sequence[UserSession]) -> list[UserSession]:
    """Return only sessions whose last heartbeat is within the liveness window.

    Drops dead sessions (shut down, killed, or wedged) whose KV entry has not
    yet hit the longer storage TTL, so every presence surface (``who``,
    ``finger``) reflects who is actually reachable.
    """
    now = datetime.now(UTC)
    return [
        s for s in sessions if s.is_live(now=now, ttl_seconds=PRESENCE_LIVENESS_SECONDS)
    ]


def dead_sessions(sessions: Sequence[UserSession]) -> list[UserSession]:
    """Return KV rows that outlived the process that wrote them.

    A session that shuts down cleanly deletes its own KV row and writes a
    wtmp logout event (``server/app.py``'s exit path). A row that is still
    PRESENT here but fails :meth:`~biff.models.UserSession.is_live` at
    :data:`DEAD_REPORT_SECONDS` is therefore a session that died without
    deregistering — killed, wedged, or a host that vanished — the orphan
    signature :func:`live_sessions` silently drops (DES-057).
    """
    now = datetime.now(UTC)
    return [
        s for s in sessions if not s.is_live(now=now, ttl_seconds=DEAD_REPORT_SECONDS)
    ]
