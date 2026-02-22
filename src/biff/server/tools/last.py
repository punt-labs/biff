"""Session history tool — ``/last``.

Shows login/logout history from the wtmp stream, matching Unix ``last(1)``.
Each row shows who logged in, from where, when, and for how long.
Sessions still active are marked "still logged in".
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from biff.models import SessionEvent
from biff.server.tools._session import update_current_session

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


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
    events: list[SessionEvent], active_keys: set[str]
) -> list[tuple[SessionEvent, SessionEvent | None]]:
    """Pair login events with their corresponding logout events.

    Returns a list of ``(login, logout | None)`` tuples sorted by
    login time descending.  A ``None`` logout means the session is
    still active or no logout was recorded.
    """
    # Index logouts by session_key — most recent first (events are
    # already sorted most-recent-first from get_wtmp).
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
        # Find matching logout: first logout with timestamp >= login timestamp
        matching_logout: SessionEvent | None = None
        if key in logouts:
            for i, lo in enumerate(logouts[key]):
                if lo.timestamp >= login.timestamp:
                    matching_logout = lo
                    logouts[key].pop(i)
                    break
        # If no matching logout and session is active, it's "still logged in"
        if matching_logout is None and key in active_keys:
            pass  # None signals "still logged in"
        pairs.append((login, matching_logout))

    return pairs


def _format_table(
    pairs: list[tuple[SessionEvent, SessionEvent | None]],
    active_keys: set[str],
) -> str:
    """Build a columnar table matching Unix ``last(1)`` style."""
    rows: list[tuple[str, str, str, str, str, str]] = []
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
        rows.append((name, tty, host, login_str, logout_str, duration))

    if not rows:
        return "No session history."

    name_w = max(4, max(len(r[0]) for r in rows))
    tty_w = max(3, max(len(r[1]) for r in rows))
    host_w = max(4, max(len(r[2]) for r in rows))
    login_w = max(5, max(len(r[3]) for r in rows))
    logout_w = max(6, max(len(r[4]) for r in rows))

    header = (
        f"\u25b6  {'NAME':<{name_w}}  {'TTY':<{tty_w}}  {'HOST':<{host_w}}"
        f"  {'LOGIN':<{login_w}}  {'LOGOUT':<{logout_w}}  DURATION"
    )
    lines: list[str] = []
    for name, tty, host, login_str, logout_str, duration in rows:
        lines.append(
            f"   {name:<{name_w}}  {tty:<{tty_w}}  {host:<{host_w}}"
            f"  {login_str:<{login_w}}  {logout_str:<{logout_w}}  {duration}"
        )
    return header + "\n" + "\n".join(lines)


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the last tool."""

    @mcp.tool(
        name="last",
        description="Show session login/logout history (like Unix last).",
    )
    async def last(user: str = "", count: int = 25) -> str:
        """Show recent session history."""
        await update_current_session(state)

        # Normalize user arg — strip @ prefix
        filter_user: str | None = None
        if user:
            filter_user = user.strip().lstrip("@")

        events = await state.relay.get_wtmp(user=filter_user, count=count * 2)
        if not events:
            return "No session history."

        # Get current sessions to mark active ones
        current_sessions = await state.relay.get_sessions()
        active_keys = {f"{s.user}:{s.tty}" for s in current_sessions}

        pairs = _pair_events(events, active_keys)
        # Filter to requested count (of logins, not total events)
        pairs = pairs[:count]

        return _format_table(pairs, active_keys)
