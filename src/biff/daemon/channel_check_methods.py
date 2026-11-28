"""The daemon's channel check methods: the model's acknowledgement, the doctor's ask."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, final, runtime_checkable

from biff.commands._result import CommandResult
from biff.relay_errors import RELAY_ERRORS

if TYPE_CHECKING:
    from biff.channel_check_wire import PendingCheck
    from biff.cli_context import CliContext
    from biff.daemon.params import Params
    from biff.daemon.session_verbs import SessionVerbs
    from biff.daemon.sessions import Session

__all__ = ["LIVE", "NOT_ACCEPTED", "ChannelCheckMethods"]

logger = logging.getLogger(__name__)

LIVE = "biff channel live."
NOT_ACCEPTED = "biff channel token not accepted; nothing changed."
CHECK_WAIT_SECONDS = 25
"""The longest a doctor's ask waits; the daemon's request timeout is longer."""


@runtime_checkable
class _CheckRelay(Protocol):
    """A relay that carries channel checks: only the NATS relay does."""

    async def request_channel_check(self, session_key: str) -> PendingCheck:
        """Send a request to *session_key* and return the wait for its answer."""
        ...

    async def publish_channel_check_ack(
        self, reply: str, session_key: str, token: str
    ) -> None:
        """Tell the doctor listening on *reply* that *session_key* saw the check."""
        ...


@final
class ChannelCheckMethods:
    """``channel_ack`` (the session's side) and ``channel_check`` (the doctor's)."""

    __slots__ = ()

    @staticmethod
    async def ack(session: Session, _verbs: SessionVerbs, p: Params) -> CommandResult:
        """Spend the pending token and relay one acknowledgement to the doctor.

        A wrong or repeated token changes nothing.  The relay is told only after
        the token is spent, so an acknowledgement is relayed at most once however
        often the client sends it.
        """
        token = p.text("token")
        p.done()
        reply = session.push.checks.acknowledge(token)
        if not reply:
            return CommandResult(text=NOT_ACCEPTED, json_data={"accepted": False})
        ctx = session.context
        if isinstance(ctx.relay, _CheckRelay):
            try:
                await ctx.relay.publish_channel_check_ack(reply, ctx.session_key, token)
            except RELAY_ERRORS:
                logger.warning("channel check reply not sent", exc_info=True)
                text = f"{LIVE[:-1]}, but the reply to biff doctor was not sent."
                return CommandResult(text=text, json_data={"accepted": True})
        return CommandResult(text=LIVE, json_data={"accepted": True})

    @staticmethod
    async def check(ctx: CliContext, p: Params) -> CommandResult:
        """Ask the session *to* to prove its channel events arrive; wait for it."""
        to = p.text("to")
        seconds = min(p.integer("seconds", CHECK_WAIT_SECONDS), CHECK_WAIT_SECONDS)
        p.done()
        relay = ctx.relay
        if not isinstance(relay, _CheckRelay):
            text = "A channel check requires a NATS relay connection."
            return CommandResult(text=text, json_data={"error": text}, error=True)
        pending = await relay.request_channel_check(to)
        try:
            confirmed = await pending.confirmed(max(seconds, 0))
        finally:
            await pending.close()
        text = "confirmed" if confirmed else "not confirmed"
        return CommandResult(text=text, json_data={"confirmed": confirmed})
