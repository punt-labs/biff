"""What ``talk`` did on the client's side, and why a read was refused."""

from __future__ import annotations

from typing import Self, final

from pydantic import ValidationError

from biff.cli_daemon_wire import UnreadableReplyError, WireResult
from biff.talk_reading import Phase, TalkStanding
from biff.talk_wire import Wire

__all__ = ["TalkOutcome", "TalkUnavailableError"]


class TalkUnavailableError(ConnectionError):
    """biffd refused to read talk: the session's relay carries none."""


@final
class TalkOutcome(Wire):
    """What ``talk`` did: the words to show, and where the conversation stands."""

    text: str
    error: bool
    phase: Phase = "idle"
    peer: str = ""

    @classmethod
    def of(cls, wire: WireResult) -> Self:
        """Return the outcome *wire* describes.

        Raises:
            UnreadableReplyError: The standing the reply carries is not one.
        """
        standing = wire.data if isinstance(wire.data, dict) else {}
        try:
            return cls.model_validate(
                {"text": wire.text, "error": wire.error, **standing}
            )
        except ValidationError as exc:
            msg = "biffd sent a talk reply this biff cannot read"
            raise UnreadableReplyError(msg) from exc

    @property
    def standing(self) -> TalkStanding:
        """Where the conversation stands after the call."""
        return TalkStanding(phase=self.phase, peer=self.peer)
