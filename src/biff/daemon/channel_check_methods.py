"""The daemon's channel check methods: the model's acknowledgement, the doctor's ask."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, final, runtime_checkable

from biff.channel_check_pending import CheckLostError, CheckRefusedError
from biff.commands._result import CommandResult
from biff.relay_errors import RELAY_ERRORS

if TYPE_CHECKING:
    from biff.channel_check_pending import PendingCheck
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

        A wrong or repeated token changes nothing.  The token is spent before the
        relay is told, so an acknowledgement is relayed at most once however often
        the client sends it; when the relay cannot be told the token is pending
        again and the answer is an error, so the model's retry is accepted.  The
        answer is ``LIVE`` only after the reply was published.
        """
        token = p.text("token")
        p.done()
        ctx = session.context
        if not isinstance(ctx.relay, _CheckRelay):
            return ChannelCheckMethods._unsent("this session has no NATS relay")
        checks = session.push.checks
        reply = checks.acknowledge(token)
        if not reply:
            return CommandResult(text=NOT_ACCEPTED, json_data={"accepted": False})
        try:
            await ctx.relay.publish_channel_check_ack(reply, ctx.session_key, token)
        except RELAY_ERRORS as exc:
            logger.warning("channel check reply not sent", exc_info=True)
            checks.restore(token, reply)
            return ChannelCheckMethods._unsent(str(exc) or type(exc).__name__)
        return CommandResult(text=LIVE, json_data={"accepted": True})

    @staticmethod
    def _unsent(why: str) -> CommandResult:
        text = (
            "biff channel not confirmed: the reply to biff doctor was not sent "
            f"({why}); call channel_ack again."
        )
        return CommandResult(text=text, json_data={"accepted": False}, error=True)

    @staticmethod
    async def check(ctx: CliContext, p: Params) -> CommandResult:
        """Ask the session *to* to prove its channel events arrive; wait for it.

        An error result says why the answer is not a confirmation: the session's
        daemon refused the request, or the relay link was lost while waiting.
        Only silence ends as ``confirmed: false``.
        """
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
        except CheckRefusedError as exc:
            text = f"refused: {exc}"
            return CommandResult(text=text, json_data={"error": text}, error=True)
        except CheckLostError as exc:
            return CommandResult(
                text=str(exc), json_data={"error": str(exc)}, error=True
            )
        finally:
            await pending.close()
        text = "confirmed" if confirmed else "not confirmed"
        return CommandResult(text=text, json_data={"confirmed": confirmed})
