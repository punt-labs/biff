"""The daemon's talk methods: invite or reply, read what arrived, end."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, final

import biff.commands.talk as talk_commands
from biff.commands import CommandResult
from biff.commands._session import update_current_session
from biff.commands.talk_read import TalkReadout
from biff.daemon.talk_feed import TalkTransport
from biff.talk_types import TalkPhase

if TYPE_CHECKING:
    from biff.cli_context import CliContext
    from biff.daemon.methods import Params

__all__ = ["TalkMethods"]


@final
class TalkMethods:
    """``talk``, ``talk_read`` and ``talk_end``, each run for one session.

    Talk rides NATS core pub/sub, so a relay that does not carry it (the local
    relay) is refused with one message rather than answered with an empty talk.
    """

    NEEDS_NATS: ClassVar[CommandResult] = CommandResult(
        text="Talk requires a NATS relay connection.", error=True
    )

    @staticmethod
    async def talk(ctx: CliContext, p: Params) -> CommandResult:
        """Accept an invite, send a line while connected, or invite."""
        to, message = p.text("to"), p.text("message", "")
        p.done()
        if not isinstance(ctx.relay, TalkTransport):
            return TalkMethods.NEEDS_NATS
        await update_current_session(ctx)
        return await talk_commands.talk(ctx, to, message)

    @staticmethod
    async def talk_read(ctx: CliContext, p: Params) -> CommandResult:
        """Drain the held talk state: invites, lines and a hangup."""
        p.done()
        if not isinstance(ctx.relay, TalkTransport):
            return TalkMethods.NEEDS_NATS
        return (await TalkReadout.take(ctx)).result()

    @staticmethod
    async def talk_end(ctx: CliContext, p: Params) -> CommandResult:
        """Hang up, or withdraw an outstanding invite."""
        p.done()
        was_talking = ctx.talk.phase is not TalkPhase.IDLE
        await update_current_session(ctx)
        ended = await talk_commands.end_or_cancel(ctx)
        return CommandResult(
            text=ended.text, json_data={"ended": was_talking}, error=ended.error
        )
