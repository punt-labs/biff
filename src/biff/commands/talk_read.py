"""A talk read: what the held talk state surfaced, as text and as wire data."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self, final

import biff.commands.talk as talk_commands
from biff.commands._result import CommandResult
from biff.formatting import (
    HEADER_PREFIX,
    format_talk_end,
    terminal_safe,
    visible_text,
)
from biff.talk_reading import TalkReading, TalkStanding
from biff.talk_types import TalkPhase
from biff.talk_wire import WireInvite, WireMessage

if TYPE_CHECKING:
    from biff.commands.talk import TalkContext
    from biff.talk_state import TalkState
    from biff.talk_types import AgentDrain, PendingInvite, TalkNotification

__all__ = ["TalkReadout"]


@final
@dataclass(frozen=True)
class TalkReadout:
    """One drain of the talk state: pending invites, lines and a hangup.

    ``accept_published`` is ``False`` only when a mutual-invite auto-accept
    could not be sent, in which case the text says so: the reader cannot see the
    log, and must not believe a conversation is open that the partner never
    joined.
    """

    NO_ACTIVITY: ClassVar[str] = "No pending talk activity."

    drain: AgentDrain
    accept_published: bool = True
    standing: TalkStanding = field(
        default_factory=lambda: TalkStanding(phase="idle", peer="")
    )
    opening: str = ""
    opening_unsent: bool = False
    dropped: int = 0
    ready: bool = False

    @classmethod
    async def take(cls, ctx: TalkContext) -> Self:
        """Drain *ctx*'s talk state, and send the accept a mutual glare owes."""
        talk = ctx.talk
        drain = talk.drain_for_agent()
        published = await cls.publish_owed_accept(ctx, drain)
        opening = await talk_commands.send_opening(ctx)
        return cls(
            drain,
            accept_published=published,
            standing=cls.standing_of(talk),
            opening=opening,
            opening_unsent=bool(talk.opening),
            dropped=talk.take_dropped(),
            ready=talk.subscribed,
        )

    @staticmethod
    def standing_of(talk: TalkState) -> TalkStanding:
        """Return where *talk*'s conversation stands now."""
        if talk.phase is TalkPhase.IDLE:
            return TalkStanding(phase="idle", peer="")
        if talk.phase is TalkPhase.INVITING:
            return TalkStanding(phase="inviting", peer=talk.partner_display)
        return TalkStanding(phase="connected", peer=talk.partner_display)

    @staticmethod
    async def publish_owed_accept(ctx: TalkContext, drain: AgentDrain) -> bool:
        """Publish the accept a higher-key mutual-glare auto-accept owes.

        ``drain_for_agent`` connects the higher-key side on a mutual glare but
        cannot publish, because it is pure state.  The lower-key partner
        connects only on receiving the accept (talk.tex ``MutualAutoAccept``),
        so a dropped accept would strand it and silently drop our messages.

        Returns ``True`` when there was no glare to answer, or the accept went
        out; ``False`` only after both attempts failed.
        """
        notif = drain.auto_accept
        if notif is None:
            return True
        return await talk_commands.publish_auto_accept(ctx, to_key=notif.nfrom_key)

    def rendered(self) -> str:
        """Render the drain for a model's context or a plain terminal.

        Pending invites come first, each with a runnable accept command that
        names the inviter's session, then queued messages and a hangup line.
        The text is single-line on purpose: it is injected into a model's
        context, so it skips the wrapping and hang indent a terminal render
        adds.  A frame with no body (an accept, an invite with no opening line)
        contributes nothing; a frame whose body is invisible after
        neutralisation still names its sender, so the reader sees that
        something arrived.
        """
        lines: list[str] = [
            f"{HEADER_PREFIX}{terminal_safe(invite.user)} wants to talk — "
            f"{terminal_safe(invite.accept_command)} to accept"
            for _user, invite in sorted(self.drain.pending.items())
        ]
        for notif in self.drain.messages:
            if notif.is_end:
                lines.append(format_talk_end(notif.sender_label))
            elif notif.nbody:
                body = visible_text(notif.nbody)
                lines.append(f"{HEADER_PREFIX}{notif.sender_label}: {body}")
        return "\n".join(lines)

    def text(self) -> str:
        """Return :meth:`rendered`, or the no-activity notice, and any warning."""
        shown = self.rendered() or self.NO_ACTIVITY
        if self.accept_published:
            return shown
        notif = self.drain.auto_accept
        partner = notif.nfrom if notif is not None else "the partner"
        if notif is not None and notif.nfrom_tty:
            partner = f"{notif.nfrom}:{notif.nfrom_tty}"
        warning = (
            f"⚠ Couldn't confirm {terminal_safe(partner)} joined the talk — they "
            "may not have connected; send a message or talk_end and retry."
        )
        return f"{shown}\n{warning}"

    def result(self) -> CommandResult:
        """Return the read as a command result.

        ``lines`` is :meth:`rendered`, one entry per line; ``messages`` and
        ``invites`` hold the same facts as fields, for a client that draws them
        itself.  ``ended`` is true when the partner hung up during this read;
        ``phase`` and ``peer`` say where the conversation stands afterwards.
        """
        shown = self.rendered()
        reading = TalkReading(
            lines=tuple(shown.split("\n")) if shown else (),
            messages=tuple(self._message(n) for n in self.drain.messages),
            invites=tuple(
                self._invite(invite)
                for _user, invite in sorted(self.drain.pending.items())
            ),
            ended=any(n.is_end for n in self.drain.messages),
            accept_published=self.accept_published,
            opening=self.opening,
            opening_unsent=self.opening_unsent,
            dropped=self.dropped,
            ready=self.ready,
            phase=self.standing.phase,
            peer=self.standing.peer,
        )
        return CommandResult(
            text=self.text(), json_data=reading.model_dump(mode="json", by_alias=True)
        )

    def _invite(self, invite: PendingInvite) -> WireInvite:
        return WireInvite.model_validate(
            {
                "from": invite.user,
                "tty": invite.tty,
                "label": self._label(invite),
                "accept": invite.accept_command,
            }
        )

    @staticmethod
    def _label(invite: PendingInvite) -> str:
        user, tty = terminal_safe(invite.user), terminal_safe(invite.tty)
        return f"{user}:{tty}" if tty else user

    @staticmethod
    def _message(notif: TalkNotification) -> WireMessage:
        return WireMessage(
            kind="end" if notif.is_end else "message",
            label=notif.sender_label,
            body=notif.nbody,
        )
