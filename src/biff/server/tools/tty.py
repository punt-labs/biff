"""Session naming tool — ``/tty [name]``.

Names the current session for easier identification in ``/who`` output.
Without arguments, auto-assigns the next sequential ``ttyN``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from biff.server.tools._activate import auto_enable
from biff.server.tools._descriptions import (
    get_tty_name,
    refresh_read_messages,
    set_tty_name,
)
from biff.server.tools._session import update_current_session
from biff.tty import claim_tty_name, validate_tty_name

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

logger = logging.getLogger(__name__)


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the tty tool."""

    @mcp.tool(
        name="tty",
        description=(
            "Name the current session. Visible in /who and /finger TTY column."
        ),
    )
    @auto_enable(state)
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

        # Release old name reservation before claiming new one (DES-035).
        old_name = get_tty_name()
        if old_name:
            try:
                await state.relay.release_tty_name(state.config.user, old_name)
            except Exception:  # noqa: BLE001
                logger.debug("Failed to release old TTY name %s", old_name)

        try:
            if name:
                claimed = await claim_tty_name(
                    state.relay, state.config.user, state.session_key, preferred=name
                )
            else:
                claimed = await claim_tty_name(
                    state.relay, state.config.user, state.session_key
                )
        except ValueError:
            # Re-reserve old name on failure.
            if old_name:
                try:
                    await state.relay.reserve_tty_name(
                        state.config.user, old_name, state.session_key
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("Failed to re-reserve old TTY name %s", old_name)
            return f"Error: name {name!r} already in use by another session."

        set_tty_name(claimed)
        await update_current_session(state, tty_name=claimed)
        await refresh_read_messages(mcp, state)
        return f"TTY: {claimed}"
