"""Domain-level formatting functions for biff output.

Shared by both MCP tool modules and CLI commands. These compose
the primitive table/column layer in ``biff._formatting``
into tool-specific output: who tables, finger blocks, last history,
wall banners, and read message tables.

The primitive layer (``ColumnSpec``, ``format_table``, ``format_idle``,
``last_component``) lives in ``biff._formatting``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from biff._formatting import (
    ColumnSpec,
    format_idle,
    format_table,
    last_component,
)
from biff.models import Message, SessionEvent, UserSession, WallPost

__all__ = [
    "LAST_SPECS",
    "READ_SPECS",
    "WHO_SPECS",
    "ColumnSpec",
    "format_finger",
    "format_finger_multi",
    "format_idle",
    "format_last",
    "format_read",
    "format_remaining",
    "format_table",
    "format_tty_block",
    "format_user_header",
    "format_wall",
    "format_who",
    "last_component",
    "pair_events",
    "parse_duration",
    "sanitize_wall_message",
]

# ---------------------------------------------------------------------------
# Duration parsing (shared by wall CLI + MCP tool)
# ---------------------------------------------------------------------------

_DURATION_UNITS: dict[str, int] = {"m": 60, "h": 3600, "d": 86400}
_MAX_DURATION = timedelta(days=3)  # Capped by sessions KV bucket TTL
_DEFAULT_DURATION = timedelta(hours=1)


def parse_duration(s: str) -> timedelta:
    """Parse a human duration string like ``30m``, ``2h``, ``1d``.

    Returns :data:`_DEFAULT_DURATION` (1 hour) when *s* is empty.
    """
    s = s.strip().lower()
    if not s:
        return _DEFAULT_DURATION
    if len(s) < 2 or s[-1] not in _DURATION_UNITS:
        msg = f"Unrecognized duration {s!r}. Use 30m, 2h, 1d, 3d."
        raise ValueError(msg)
    try:
        n = int(s[:-1])
    except ValueError:
        msg = f"Unrecognized duration {s!r}. Use 30m, 2h, 1d, 3d."
        raise ValueError(msg) from None
    if n <= 0:
        msg = "Duration must be positive."
        raise ValueError(msg)
    try:
        td = timedelta(seconds=n * _DURATION_UNITS[s[-1]])
    except OverflowError:
        msg = f"Duration {s!r} exceeds maximum of 3 days."
        raise ValueError(msg) from None
    if td > _MAX_DURATION:
        msg = f"Duration {s!r} exceeds maximum of 3 days."
        raise ValueError(msg)
    return td


def format_remaining(expires_at: datetime) -> str:
    """Human-readable time remaining until *expires_at*."""
    remaining = expires_at - datetime.now(UTC)
    total_seconds = int(remaining.total_seconds())
    if total_seconds <= 0:
        return "expired"
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours > 0:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m" if minutes else "<1m"


# ---------------------------------------------------------------------------
# /who — session list
# ---------------------------------------------------------------------------

WHO_SPECS: list[ColumnSpec] = [
    ColumnSpec("NAME", min_width=4),
    ColumnSpec("TTY", min_width=3),
    ColumnSpec("IDLE", min_width=4),
    ColumnSpec("S", min_width=1),
    ColumnSpec("HOST", min_width=4),
    ColumnSpec("DIR", min_width=3),
    ColumnSpec("PLAN", min_width=10, fixed=False),
]


def _sanitize_plan(plan: str) -> str:
    """Collapse newlines so plan text stays on one row."""
    return plan.replace("\n", " ").replace("\r", " ")


def format_who(sessions: list[UserSession]) -> str:
    """Build a columnar table matching ``w(1)`` style with host and dir."""
    rows: list[list[str]] = [
        [
            f"@{s.user}",
            s.tty_name or (s.tty[:8] if s.tty else "-"),
            format_idle(s.last_active),
            "+" if s.biff_enabled else "-",
            s.hostname or "-",
            last_component(s.pwd) if s.pwd else "-",
            _sanitize_plan(s.plan) if s.plan else "(no plan)",
        ]
        for s in sessions
    ]
    return format_table(WHO_SPECS, rows)


# ---------------------------------------------------------------------------
# /finger — user status query
# ---------------------------------------------------------------------------


def _format_finger_idle(dt: datetime) -> str:
    """Format idle time matching BSD ``finger(1)`` style.

    Examples: ``0:03``, ``3:45``, ``1 day 7:22``
    """
    now = datetime.now(UTC)
    total_seconds = max(0, int((now - dt).total_seconds()))
    minutes = total_seconds // 60
    hours = minutes // 60
    days = hours // 24

    if days > 0:
        return f"{days} day{'s' if days > 1 else ''} {hours % 24}:{minutes % 60:02d}"
    return f"{hours}:{minutes % 60:02d}"


def format_user_header(session: UserSession) -> str:
    """Format the user-level header (shown once per user)."""
    left = f"Login: {session.user}"
    mesg = "on" if session.biff_enabled else "off"
    if session.display_name:
        right = f"Name: {session.display_name}"
        line1 = f"\u25b6  {left:<38s}{right}"
        line2 = f"   Messages: {mesg}"
        return f"{line1}\n{line2}"
    right = f"Messages: {mesg}"
    return f"\u25b6  {left:<38s}{right}"


def format_tty_block(session: UserSession) -> str:
    """Format per-TTY details (on-since, host/dir, plan)."""
    idle = _format_finger_idle(session.last_active)
    since = session.last_active.strftime("%a %b %d %H:%M (%Z)")
    tty_label = session.tty_name or (session.tty[:8] if session.tty else "?")

    lines = [f"   On since {since} on {tty_label}, idle {idle}"]
    if session.hostname or session.pwd:
        host = session.hostname or "?"
        pwd = session.pwd or "?"
        lines.append(f"   Host: {host}  Dir: {pwd}")
    lines.append(f"   Plan:\n    {session.plan}" if session.plan else "   No Plan.")
    return "\n".join(lines)


def format_finger(session: UserSession) -> str:
    """Format a single session in BSD ``finger(1)`` style."""
    return f"{format_user_header(session)}\n{format_tty_block(session)}"


def format_finger_multi(sessions: list[UserSession]) -> str:
    """Format all sessions for a user (header once, multiple tty blocks)."""
    by_idle = sorted(sessions, key=lambda s: s.last_active, reverse=True)
    header = format_user_header(by_idle[0])
    tty_blocks = [format_tty_block(s) for s in by_idle]
    return header + "\n" + "\n".join(tty_blocks)


# ---------------------------------------------------------------------------
# /last — session history
# ---------------------------------------------------------------------------

LAST_SPECS: list[ColumnSpec] = [
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


def pair_events(
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


def format_last(
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

    return format_table(LAST_SPECS, rows)


# ---------------------------------------------------------------------------
# /wall — team broadcast
# ---------------------------------------------------------------------------

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_wall_message(message: str) -> str:
    """Strip control chars and collapse whitespace for wall messages."""
    message = _CTRL_RE.sub("", message)
    return " ".join(message.split())


def format_wall(wall: WallPost) -> str:
    """Format a wall post for display."""
    remaining = format_remaining(wall.expires_at)
    sender = f"@{wall.from_user}"
    if wall.from_tty:
        sender += f" ({wall.from_tty})"
    return f"\u25b6  WALL from {sender} ({remaining} remaining)\n   {wall.text}"


# ---------------------------------------------------------------------------
# /read — message inbox
# ---------------------------------------------------------------------------

READ_SPECS: list[ColumnSpec] = [
    ColumnSpec("FROM", min_width=4),
    ColumnSpec("DATE", min_width=16),
    ColumnSpec("MESSAGE", min_width=10, fixed=False),
]


def format_read(messages: list[Message]) -> str:
    """Format messages in BSD ``from(1)`` style."""
    rows: list[list[str]] = []
    for m in messages:
        ts = m.timestamp.strftime("%a %b %d %H:%M")
        rows.append([m.from_user, ts, m.body])
    return format_table(READ_SPECS, rows)
