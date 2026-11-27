"""A talk read: what the held talk state surfaced, as text and as wire data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self, final

import biff.commands.talk as talk_commands
from biff.commands._result import CommandResult
from biff.formatting import (
    HEADER_PREFIX,
    format_talk_end,
    terminal_safe,
    visible_text,
)
from biff.talk_types import TalkPhase

if TYPE_CHECKING:
    from biff.commands.talk import TalkContext
    from biff.talk_state import TalkState
    from biff.talk_types import AgentDrain, PendingInvite, TalkNotification

__all__ = ["TalkReadout", "TalkStanding"]


@final
@dataclass(frozen=True)
class TalkStanding:
    """Where a conversation stands: its phase, and with whom unless idle."""

    phase: str
    peer: str

    @classmethod
    def of(cls, talk: TalkState) -> Self:
        """Return the standing of *talk* now."""
        if talk.phase is TalkPhase.IDLE:
            return cls("idle", "")
        return cls(talk.phase.name.lower(), talk.partner_display)

    @property
    def data(self) -> dict[str, str]:
        """The standing as wire fields."""
        return {"phase": self.phase, "peer": self.peer}


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
    standing: TalkStanding = TalkStanding("idle", "")

    @classmethod
    async def take(cls, ctx: TalkContext) -> Self:
        """Drain *ctx*'s talk state, and send the accept a mutual glare owes."""
        talk = ctx.talk
        drain = talk.drain_for_agent()
        return cls(
            drain,
            accept_published=await cls.publish_owed_accept(ctx, drain),
            standing=TalkStanding.of(talk),
        )

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
        messages = [self._message(n) for n in self.drain.messages]
        data: dict[str, object] = {
            "lines": shown.split("\n") if shown else [],
            "messages": messages,
            "invites": [
                {
                    "from": invite.user,
                    "tty": invite.tty,
                    "label": self._label(invite),
                    "accept": invite.accept_command,
                }
                for _user, invite in sorted(self.drain.pending.items())
            ],
            "ended": any(n.is_end for n in self.drain.messages),
            "accept_published": self.accept_published,
            **self.standing.data,
        }
        return CommandResult(text=self.text(), json_data=data)

    @staticmethod
    def _label(invite: PendingInvite) -> str:
        user, tty = terminal_safe(invite.user), terminal_safe(invite.tty)
        return f"{user}:{tty}" if tty else user

    @staticmethod
    def _message(notif: TalkNotification) -> dict[str, str]:
        return {
            "kind": "end" if notif.is_end else "message",
            "label": notif.sender_label,
            "body": notif.nbody,
        }
