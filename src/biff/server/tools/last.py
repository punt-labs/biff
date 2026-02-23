"""Session history tool — ``/last``.

Shows login/logout history from the wtmp stream, matching Unix ``last(1)``.
Each row shows who logged in, from where, when, and for how long.
Sessions still active are marked "still logged in".
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from biff.models import SessionEvent
from biff.server.tools._activate import lazy_activate
from biff.server.tools._formatting import ColumnSpec, format_table
from biff.server.tools._session import update_current_session
from biff.tty import build_session_key

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

_LAST_SPECS: list[ColumnSpec] = [
    ColumnSpec("NAME", min_width=4),
    ColumnSpec("TTY", min_width=3),
    ColumnSpec("HOST", min_width=4),
    ColumnSpec("LOGIN", min_width=16),
    ColumnSpec("LOGOUT", min_width=15),
    ColumnSpec("DURATION", min_width=8, fixed=False),
]


def _format_duration(login_ts: datetime, logout_ts: datetime) -> str:
    """Format duration between login and logout as ``H:MM``."""
    total_seconds = max(0, int((logout_ts - login_ts).total_seconds()))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    return f"{hours}:{minutes:02d}"


def _format_timestamp(dt: datetime) -> str:
    """Format a datetime as ``Mon Feb 22 14:01``."""
    return dt.strftime("%a %b %d %H:%M")


def _pair_events(
    events: list[SessionEvent],
) -> list[tuple[SessionEvent, SessionEvent | None]]:
    """Pair login events with their corresponding logout events.

    Returns a list of ``(login, logout | None)`` tuples sorted by
    login time descending.  A ``None`` logout means the session is
    still active or no logout was recorded — the caller uses
    ``active_keys`` to distinguish between the two.
    """
    logouts: dict[str, list[SessionEvent]] = {}
    logins: list[SessionEvent] = []

    for event in events:
        if event.event == "logout":
            logouts.setdefault(event.session_key, []).append(event)
        else:
            logins.append(event)

    pairs: list[tuple[SessionEvent, SessionEvent | None]] = []
    for login in logins:
        key = login.session_key
        matching_logout: SessionEvent | None = None
        if key in logouts:
            for i, lo in enumerate(logouts[key]):
                if lo.timestamp >= login.timestamp:
                    matching_logout = lo
                    logouts[key].pop(i)
                    break
        pairs.append((login, matching_logout))

    return pairs


def _format_last(
    pairs: list[tuple[SessionEvent, SessionEvent | None]],
    active_keys: set[str],
) -> str:
    """Build a columnar table matching Unix ``last(1)`` style."""
    rows: list[list[str]] = []
    for login, logout in pairs:
        name = f"@{login.user}"
        tty = login.tty_name or (login.tty[:8] if login.tty else "-")
        host = login.hostname or "-"
        login_str = _format_timestamp(login.timestamp)
        if logout is not None:
            logout_str = _format_timestamp(logout.timestamp)
            duration = _format_duration(login.timestamp, logout.timestamp)
        elif login.session_key in active_keys:
            logout_str = "still logged in"
            duration = "-"
        else:
            logout_str = "gone"
            duration = "-"
        rows.append([name, tty, host, login_str, logout_str, duration])

    if not rows:
        return "No session history."

    return format_table(_LAST_SPECS, rows)


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the last tool."""

    @mcp.tool(
        name="last",
        description="Show session login/logout history (like Unix last).",
    )
    async def last(user: str = "", count: int = 25) -> str:
        """Show recent session history."""
        msg = lazy_activate(state)
        if msg:
            return msg
        await update_current_session(state)
        count = max(1, min(count, 100))

        # Normalize user arg — strip @ prefix
        filter_user: str | None = None
        if user:
            filter_user = user.strip().lstrip("@")

        events = await state.relay.get_wtmp(user=filter_user, count=count * 2)
        if not events:
            return "No session history."

        current_sessions = await state.relay.get_sessions()
        active_keys = {build_session_key(s.user, s.tty) for s in current_sessions}

        pairs = _pair_events(events)
        pairs = pairs[:count]

        return _format_last(pairs, active_keys)
