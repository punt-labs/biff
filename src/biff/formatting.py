"""Domain-level formatting functions for biff output.

Shared by both MCP tool modules and CLI commands. These compose
the primitive table/column layer in ``biff._formatting``
into tool-specific output: who tables, finger blocks, last history,
wall banners, and read message tables.

The primitive layer (``ColumnSpec``, ``format_table``, ``format_idle``,
``last_component``) lives in ``biff._formatting``.
"""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime, timedelta

from biff._formatting import (
    HEADER_PREFIX,
    ROW_PREFIX,
    TABLE_WIDTH,
    ColumnSpec,
    format_idle,
    format_table,
    last_component,
    terminal_safe,
    visible_width,
)
from biff._stdlib import display_repo_name
from biff.models import Message, SessionEvent, UserSession, WallPost

__all__ = [
    "HEADER_PREFIX",
    "LAST_SPECS",
    "READ_SPECS",
    "WHO_SPECS",
    "ColumnSpec",
    "format_finger",
    "format_finger_multi",
    "format_idle",
    "format_last",
    "format_read",
    "format_read_dual",
    "format_remaining",
    "format_table",
    "format_talk_end",
    "format_talk_line",
    "format_tty_block",
    "format_user_header",
    "format_wall",
    "format_who",
    "last_component",
    "pair_events",
    "parse_duration",
    "sanitize_wall_message",
    "terminal_safe",
]

# Never wrap a talk body narrower than this, however long the sender
# label + timestamp lead grows.
_TALK_WRAP_MIN = 24

# Cap the rendered sender label so a forged, boundary-slipped label can never
# drive the per-line wrap indent (defense in depth for the O(label x body)
# amplification — see TalkNotification.from_payload, biff-7g7).
_MAX_LABEL_WIDTH = 40


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
    ColumnSpec("NAME", min_width=10),
    ColumnSpec("K", min_width=1),
    ColumnSpec("REPO", min_width=4),
    ColumnSpec("IDLE", min_width=4),
    ColumnSpec("S", min_width=1),
    ColumnSpec("P", min_width=1),
    ColumnSpec("HOST", min_width=4),
]


def _format_who_name(s: UserSession) -> str:
    """Render a session as a copy-pasteable address for ``/write``."""
    tty = terminal_safe(s.tty_name) or (s.tty[:8] if s.tty else "")
    user = terminal_safe(s.user)
    return f"{user}:{tty}" if tty else user


def _format_who_kind(s: UserSession) -> str:
    """Render a kind tag for the ``/who`` table. Empty for humans."""
    if s.kind == "agent":
        return "[A]"
    return ""


def format_who(sessions: list[UserSession]) -> str:
    """Build a columnar table matching BSD ``w(1)`` style.

    The NAME column renders ``user:tty`` so agents can copy the
    address directly into ``/write``.  P column shows ``+`` if the
    session has a plan, ``-`` otherwise.  Use ``/finger user`` to
    see the full plan text.
    """
    rows: list[list[str]] = [
        [
            _format_who_name(s),
            _format_who_kind(s),
            display_repo_name(s.repo) or "-",
            format_idle(s.last_active),
            "+" if s.biff_enabled else "-",
            "+" if s.plan else "-",
            terminal_safe(s.hostname) or "-",
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
    user = terminal_safe(session.user)
    login_label = user
    if session.kind:
        login_label = f"{user} [{terminal_safe(session.kind)}]"
    left = f"Login: {login_label}"
    mesg = "on" if session.biff_enabled else "off"
    if session.display_name:
        right = f"Name: {terminal_safe(session.display_name)}"
        line1 = f"\u25b6  {left:<38s}{right}"
        line2 = f"   Messages: {mesg}"
        return f"{line1}\n{line2}"
    right = f"Messages: {mesg}"
    return f"\u25b6  {left:<38s}{right}"


def format_tty_block(session: UserSession) -> str:
    """Format per-TTY details (on-since, host/dir, plan)."""
    idle = _format_finger_idle(session.last_active)
    since = session.last_active.strftime("%a %b %d %H:%M (%Z)")
    tty_label = terminal_safe(session.tty_name) or (
        session.tty[:8] if session.tty else "?"
    )

    lines = [f"   On since {since} on {tty_label}, idle {idle}"]
    if session.hostname or session.pwd:
        host = terminal_safe(session.hostname) or "?"
        pwd = terminal_safe(session.pwd) or "?"
        lines.append(f"   Host: {host}  Dir: {pwd}")
    plan = terminal_safe(session.plan)
    lines.append(f"   Plan:\n    {plan}" if plan else "   No Plan.")
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
    ColumnSpec("NAME", min_width=10),
    ColumnSpec("REPO", min_width=4),
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

    Returns a list of ``(login, logout | None)`` tuples in the same
    relative order as their login events appear in *events*.  A
    ``None`` logout means the session is still active or no logout
    was recorded — the caller uses ``active_keys`` to distinguish
    between the two.
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
        tty = terminal_safe(login.tty_name) or (login.tty[:8] if login.tty else "")
        user = terminal_safe(login.user)
        name = f"{user}:{tty}" if tty else user
        repo = display_repo_name(login.repo) or "-"
        host = terminal_safe(login.hostname) or "-"
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
        rows.append([name, repo, host, login_str, logout_str, duration])

    if not rows:
        return "No session history."

    return format_table(LAST_SPECS, rows)


# ---------------------------------------------------------------------------
# /wall — team broadcast
# ---------------------------------------------------------------------------


def sanitize_wall_message(message: str) -> str:
    """Normalize a wall message for posting.

    Strips all non-printable characters via :func:`terminal_safe` (control,
    ESC/OSC introducers, and other C0/C1 chars) and collapses runs of
    whitespace to single spaces.  This is input-side hygiene; render sites
    sanitize again at the output boundary (biff-lbj).
    """
    return " ".join(terminal_safe(message).split())


def format_wall(wall: WallPost) -> str:
    """Format a wall post for display."""
    remaining = format_remaining(wall.expires_at)
    sender = terminal_safe(wall.from_user)
    if wall.from_tty:
        sender += f" ({terminal_safe(wall.from_tty)})"
    text = terminal_safe(wall.text)
    return f"\u25b6  WALL from {sender} ({remaining} remaining)\n   {text}"


# ---------------------------------------------------------------------------
# talk \u2014 conversation lines
# ---------------------------------------------------------------------------


def format_talk_line(label: str, body: str, *, stamp: str = "") -> list[str]:
    """Render one incoming talk line in the biff ``\u25b6`` idiom, wrapped.

    Matches the ``who``/``read``/``wall`` convention: a leading ``\u25b6``
    prefix, the sender's ``user:tty`` address, then the message \u2014 wrapped
    to :data:`TABLE_WIDTH` with continuation lines aligned under the body.
    *stamp* is the caller's ``[HH:MM] `` prefix (empty when timestamps are
    off).  All remote-controlled text is neutralised here via
    :func:`terminal_safe`, the output boundary (biff-lbj).

    Returns one string per rendered line so the caller can colourise each, or
    an empty list when the body is empty *after* neutralisation \u2014 a
    control-only payload has nothing to show and must not render a bare lead
    (biff-7g7).

    The lead is bounded independently of the input: the label is truncated to
    :data:`_MAX_LABEL_WIDTH` and the continuation indent never exceeds
    :data:`TABLE_WIDTH`.  Without those caps a forged megabyte label would be
    copied onto every wrapped body chunk \u2014 O(label x body) allocation
    from a single frame (defense in depth behind the
    :meth:`TalkNotification.from_payload` boundary clamp).

    Precondition: *body* MUST already be boundary-clamped by the caller \u2014 the
    lead is capped here, but the body is not, so the wrap work is O(body).
    Every current caller passes the \u2264 :data:`~biff.talk_types.MAX_BODY_LEN`
    ``TalkNotification.nbody``; a future caller feeding an unclamped body from a
    non-notification source would reintroduce the linear term.
    """
    safe_body = terminal_safe(body)
    # Whitespace survives terminal_safe (spaces are printable), so guard on the
    # stripped body: an all-whitespace or control-only payload has nothing to
    # show and must render no line, never a bare arrow lead.
    if not safe_body.strip():
        return []
    safe_label = _truncate(terminal_safe(label), _MAX_LABEL_WIDTH)
    lead = f"{HEADER_PREFIX}{stamp}{safe_label}  "
    width = max(_TALK_WRAP_MIN, TABLE_WIDTH - visible_width(lead))
    # replace_whitespace=False keeps the sender's message verbatim — textwrap's
    # default would rewrite each whitespace char to a space.  terminal_safe has
    # already stripped every control char (tabs, newlines) before this point, so
    # only spaces remain: nothing can inject a line or skew the wrap width.
    chunks = textwrap.wrap(safe_body, width, replace_whitespace=False) or [""]
    indent = " " * min(visible_width(lead), TABLE_WIDTH)
    return [lead + chunks[0], *(indent + chunk for chunk in chunks[1:])]


def _truncate(text: str, width: int) -> str:
    """Return *text* clipped to *width* code points, ending with ``\u2026``.

    Clips by code-point count (``len``) as an approximation of display width.
    *text* is already neutralised (no ANSI, no control characters), so one code
    point is usually one column \u2014 but wide glyphs (emoji, CJK) occupy more than
    one terminal column, so the clip is not column-accurate for them.
    """
    if len(text) <= width:
        return text
    return text[: width - 1] + "\u2026"


def format_talk_end(label: str) -> str:
    """Render a partner-hangup line in the ``\u25b6`` idiom.

    The label is capped at :data:`_MAX_LABEL_WIDTH` like the
    :func:`format_talk_line` lead, so a forged sender label (bounded only by the
    :meth:`TalkNotification.from_payload` ingress clamp) cannot drive the hangup
    line past :data:`TABLE_WIDTH`.
    """
    safe_label = _truncate(terminal_safe(label), _MAX_LABEL_WIDTH)
    return f"{HEADER_PREFIX}{safe_label} has ended the conversation."


# ---------------------------------------------------------------------------
# /read — message inbox
# ---------------------------------------------------------------------------

READ_SPECS: list[ColumnSpec] = [
    ColumnSpec("FROM", min_width=10),
    ColumnSpec("DATE", min_width=16),
    ColumnSpec("MESSAGE", min_width=10, fixed=False),
]


def format_read_dual(
    human_user: str,
    human_msgs: list[Message],
    agent_user: str,
    agent_msgs: list[Message],
) -> str:
    """Format messages with per-identity section headers."""
    sections: list[str] = []
    for user, msgs in ((human_user, human_msgs), (agent_user, agent_msgs)):
        if not msgs:
            continue
        rows: list[list[str]] = []
        for m in msgs:
            ts = m.timestamp.strftime("%a %b %d %H:%M")
            fu = terminal_safe(m.from_user)
            sender = f"{fu}:{terminal_safe(m.from_tty)}" if m.from_tty else fu
            rows.append([sender, ts, terminal_safe(m.body)])
        table = format_table(READ_SPECS, rows)
        # Indent the table under the section header: replace the
        # leading HEADER_PREFIX on the column-header line with
        # ROW_PREFIX so it aligns as a sub-table row.
        indented_table = ROW_PREFIX + table[len(HEADER_PREFIX) :]
        sections.append(f"{HEADER_PREFIX}{user}\n{indented_table}")
    return "\n\n".join(sections)


def format_read(messages: list[Message]) -> str:
    """Format messages in BSD ``from(1)`` style.

    The FROM column renders a copy-pasteable reply address:
    ``user:ttyNN`` when the sender's tty is known, ``user`` otherwise.
    """
    rows: list[list[str]] = []
    for m in messages:
        ts = m.timestamp.strftime("%a %b %d %H:%M")
        fu = terminal_safe(m.from_user)
        sender = f"{fu}:{terminal_safe(m.from_tty)}" if m.from_tty else fu
        rows.append([sender, ts, terminal_safe(m.body)])
    return format_table(READ_SPECS, rows)
