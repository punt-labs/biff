"""Fetch a session's inbox and its user's broadcast inbox without losing either."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, final

from biff.relay_errors import RELAY_ERRORS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from biff.cli_context import CliContext
    from biff.models import Message

__all__ = ["UnreadFetch"]

logger = logging.getLogger(__name__)


@final
class UnreadFetch:
    """The two fetches of a read, the second failing without costing the first.

    A relay that consumes mail when it is fetched (NATS) no longer holds what
    the first fetch returned.  If the second fetch then fails, nothing is left
    to return that mail to, so it is filed again, where the next read finds it.
    """

    __slots__ = ()

    @staticmethod
    async def of(ctx: CliContext) -> tuple[list[Message], list[Message]]:
        """Return the session's mail and the broadcast mail, neither yet read."""
        tty = await ctx.relay.fetch(ctx.session_key)
        try:
            user = await ctx.relay.fetch_user_inbox(ctx.user)
        except Exception:
            await UnreadFetch._filed_again(ctx, tty)
            raise
        return tty, user

    @staticmethod
    async def _filed_again(ctx: CliContext, fetched: Sequence[Message]) -> None:
        if not ctx.relay.consumes_on_fetch:
            return
        for message in fetched:
            try:
                await ctx.relay.deliver(message.refiled())
            except RELAY_ERRORS:
                logger.warning("could not file message %s again", message.id)
