"""Rebuild this server's presence row after it was deleted underneath it.

Lifespan registration writes the row before any tool call, so a missing row
means it was removed while the process kept running: a reaper sentinel, NATS
TTL expiry or a manual delete.  The tty reservation expires with the row, so
the rebuilt row must reserve its name again, or two sessions can show the
same ``user:ttyN`` and addressed mail can reach either.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self

from biff.models import UserSession
from biff.server.tools._descriptions import get_tty_name, set_tty_name
from biff.tty import claim_tty_name

if TYPE_CHECKING:
    from biff.server.state import ServerState

logger = logging.getLogger(__name__)


class SessionRevival:
    """Rebuilds one server's presence row and re-reserves its tty name."""

    _state: ServerState

    def __new__(cls, state: ServerState) -> Self:
        """Prepare to revive *state*'s row."""
        self = super().__new__(cls)
        self._state = state
        return self

    async def revive(self) -> UserSession:
        """Write a fresh row for this server and return it.

        A stashed tty name means registration ran to completion, so the row
        was deleted afterward; the rebuilt record resets plan, plan_source and
        biff_enabled, which ``/who`` would show as a plausible but wrong row,
        so the resurrection is logged.  With no stash (startup interrupted),
        the hex-slice placeholder ``_format_who_name`` uses keeps ``tty_name``
        non-empty, as the v1.8.0 invariant requires.
        """
        state = self._state
        stashed = get_tty_name()
        if stashed:
            logger.warning(
                "Session reaped mid-run; resurrecting with default "
                "plan/biff_enabled state"
            )
            tty_name = await self._reserve(stashed)
        else:
            logger.warning(
                "Auto-creating session without a reserved tty_name; "
                "lifespan registration may not have completed"
            )
            tty_name = state.tty[:8]
        session = UserSession(
            user=state.config.user,
            tty=state.tty,
            tty_name=tty_name,
            hostname=state.hostname,
            pwd=state.pwd,
            display_name=state.config.display_name,
            kind=state.config.kind,
            repo=state.config.repo_name,
            launched_by=state.config.launched_by,
            owner_git=state.config.owner_git,
        )
        await state.relay.update_session(session)
        return session

    async def _reserve(self, name: str) -> str:
        """Reserve *name* again, or the lowest free name when another took it."""
        state = self._state
        user, key = state.config.user, state.session_key
        try:
            name = await claim_tty_name(state.relay, user, key, preferred=name)
        except ValueError:  # taken while this session's row was gone
            name = await claim_tty_name(state.relay, user, key)
        set_tty_name(name)
        return name
