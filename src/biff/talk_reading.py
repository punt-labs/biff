"""What one ``talk_read`` drains, and where a conversation stands."""

from __future__ import annotations

from typing import Literal, final

from biff.talk_wire import Wire, WireInvite, WireMessage

__all__ = ["TalkReading", "TalkStanding"]

type Phase = Literal["idle", "inviting", "connected"]


@final
class TalkStanding(Wire):
    """Where a conversation stands: its phase, and with whom unless idle."""

    phase: Phase
    peer: str


@final
class TalkReading(Wire):
    """What one ``talk_read`` drained.

    ``lines`` is the text of the read, one entry per line, for a reader that
    draws nothing itself; ``messages`` and ``invites`` hold the same facts as
    fields.  ``ended`` is true when the partner hung up during the read.
    ``opening`` is the line an accepted invite carried, sent by this read, and
    ``opening_unsent`` says one is still owed.  ``dropped`` counts the frames
    the full queue lost since the last read.  ``ready`` says the server has confirmed
    the subscription that delivers new frames; what is already held needs none.
    """

    lines: tuple[str, ...]
    messages: tuple[WireMessage, ...]
    invites: tuple[WireInvite, ...]
    ended: bool
    accept_published: bool
    opening: str
    opening_unsent: bool
    dropped: int
    ready: bool
    phase: Phase
    peer: str

    @property
    def standing(self) -> TalkStanding:
        """Where the conversation stands after the read."""
        return TalkStanding(phase=self.phase, peer=self.peer)
