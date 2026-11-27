"""What ``talk`` did, and where a conversation stands."""

from __future__ import annotations

from typing import Literal, Self, final

from biff.cli_daemon_wire import WireResult
from biff.cli_talk_wire import Wire

__all__ = ["TalkOutcome", "TalkStanding"]


@final
class TalkStanding(Wire):
    """Where a conversation stands: its phase, and with whom unless idle."""

    phase: Literal["idle", "inviting", "connected"]
    peer: str


@final
class TalkOutcome(Wire):
    """What ``talk`` did: the words to show, and where the conversation stands."""

    text: str
    error: bool
    phase: Literal["idle", "inviting", "connected"] = "idle"
    peer: str = ""

    @classmethod
    def of(cls, wire: WireResult) -> Self:
        """Return the outcome *wire* describes.

        Raises:
            UnreadableReplyError: The standing the reply carries is not one.
        """
        standing = wire.data if isinstance(wire.data, dict) else {}
        return cls.read({"text": wire.text, "error": wire.error, **standing})

    @property
    def standing(self) -> TalkStanding:
        """Where the conversation stands after the call."""
        return TalkStanding(phase=self.phase, peer=self.peer)
