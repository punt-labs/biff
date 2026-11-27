"""What one ``talk_read`` drained."""

from __future__ import annotations

from typing import Literal, Self, final

from biff.cli_daemon_wire import WireResult
from biff.cli_talk_outcome import TalkStanding
from biff.cli_talk_wire import Wire, WireInvite, WireMessage

__all__ = ["TalkReading", "TalkUnavailableError"]


class TalkUnavailableError(ConnectionError):
    """biffd refused to read talk: the session's relay carries none."""


@final
class TalkReading(Wire):
    """What one ``talk_read`` drained."""

    messages: tuple[WireMessage, ...]
    invites: tuple[WireInvite, ...]
    ended: bool
    accept_published: bool
    phase: Literal["idle", "inviting", "connected"]
    peer: str

    @classmethod
    def of(cls, wire: WireResult) -> Self:
        """Return the reading *wire* holds.

        Raises:
            TalkUnavailableError: biffd answered with a refusal.
            UnreadableReplyError: The reply lacks a field a reading has.
        """
        if wire.error:
            raise TalkUnavailableError(wire.text)
        return cls.read(wire.data)

    @property
    def standing(self) -> TalkStanding:
        """Where the conversation stands after the read."""
        return TalkStanding(phase=self.phase, peer=self.peer)
