"""User status query tool — ``/finger @user``.

Shows what a user is working on, when they were last active,
and whether they're accepting messages.  Supports ``@user``
(shows all sessions) and ``@user:tty`` (shows one session).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from biff.models import UserSession
from biff.server.tools._descriptions import refresh_read_messages
from biff.server.tools._session import resolve_session, update_current_session
from biff.tty import parse_address

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def _format_idle(dt: datetime) -> str:
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


def _format_user_header(session: UserSession) -> str:
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


def _format_tty_block(session: UserSession) -> str:
    """Format per-TTY details (on-since, host/dir, plan)."""
    idle = _format_idle(session.last_active)
    since = session.last_active.strftime("%a %b %d %H:%M (%Z)")
    tty_label = session.tty_name or (session.tty[:8] if session.tty else "?")

    lines = [f"   On since {since} on {tty_label}, idle {idle}"]
    if session.hostname or session.pwd:
        host = session.hostname or "?"
        pwd = session.pwd or "?"
        lines.append(f"   Host: {host}  Dir: {pwd}")
    lines.append(f"   Plan:\n    {session.plan}" if session.plan else "   No Plan.")
    return "\n".join(lines)


def _format_session(session: UserSession) -> str:
    """Format a single session in BSD ``finger(1)`` style."""
    return f"{_format_user_header(session)}\n{_format_tty_block(session)}"


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the finger tool."""

    @mcp.tool(
        name="finger",
        description="Check what a user is working on and their availability.",
    )
    async def finger(user: str) -> str:
        """Query a user's session and presence info.

        ``@user`` shows all sessions for that user.
        ``@user:tty`` shows a specific session.
        """
        await update_current_session(state)
        await refresh_read_messages(mcp, state)
        bare_user, tty = parse_address(user)

        if tty:
            # Targeted: resolve by hex ID or tty_name
            session = await resolve_session(state.relay, bare_user, tty)
            if session is None:
                return f"Login: {bare_user}\nNo session on tty {tty}."
            return _format_session(session)

        # Bare user: show all sessions
        sessions = await state.relay.get_sessions_for_user(bare_user)
        if not sessions:
            return f"Login: {bare_user}\nNever logged in."
        by_idle = sorted(sessions, key=lambda s: s.last_active, reverse=True)
        header = _format_user_header(by_idle[0])
        tty_blocks = [_format_tty_block(s) for s in by_idle]
        return header + "\n" + "\n".join(tty_blocks)
