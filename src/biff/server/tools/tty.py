"""Session naming tool — ``/tty [name]``.

Names the current session for easier identification in ``/who`` output.
Without arguments, auto-assigns the next sequential ``ttyN``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.server.tools._activity import track_activity
from biff.server.tools._descriptions import (
    get_tty_name,
    refresh_read_messages,
    set_tty_name,
)
from biff.server.tools._session import update_current_session
from biff.tty import rename_tty, validate_tty_name

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the tty tool."""

    @mcp.tool(
        name="tty",
        description=(
            "Name the current session. Visible in /who and /finger TTY column."
        ),
    )
    @track_activity(state)
    async def tty(name: str = "") -> str:
        """Set a human-readable name for this session.

        If *name* is omitted, auto-assigns the next ``ttyN``.
        Names are limited to 20 characters.

        Output echoes the name::

            TTY: tty3
        """
        name = name.strip()

        if name:
            error = validate_tty_name(name)
            if error:
                return f"Error: {error}"

        # Claim new name, then release old on success (DES-035).
        old_name = get_tty_name()
        try:
            claimed = await rename_tty(
                state.relay,
                state.config.user,
                state.session_key,
                old_name,
                preferred=name or None,
            )
        except ValueError:
            return f"Error: name {name!r} already in use by another session."
        except RuntimeError:
            return "Error: failed to claim TTY name after retries."

        set_tty_name(claimed)
        # Keep the shared TalkState in sync with the rename: talk invite reply
        # hints render from state.talk.my_tty_name, so a stale name here would
        # embed the old tty in the hint and the partner's suggested address would
        # no longer resolve (matches app.py registration and commands/tty.py).
        state.talk.set_tty_name(claimed)
        await update_current_session(state, tty_name=claimed)
        # Keep the resume-reclaim hint current so the next resume reclaims
        # the RENAMED alias, not the stale one. The routing token
        # is state.tty (the session_id under identity routing).
        await state.relay.set_session_tty_hint(state.config.user, state.tty, claimed)
        await refresh_read_messages(mcp, state)
        return f"TTY: {claimed}"
