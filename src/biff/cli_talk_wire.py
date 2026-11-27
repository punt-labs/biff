"""What biffd answers the talk methods with, read tolerantly."""

from __future__ import annotations

from typing import Literal, Self, final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from biff.cli_daemon_wire import UnreadableReplyError, WireResult

__all__ = [
    "TalkOutcome",
    "TalkReading",
    "TalkStanding",
    "TalkUnavailableError",
    "WireInvite",
    "WireMessage",
]


class TalkUnavailableError(ConnectionError):
    """biffd refused to read talk: the session's relay carries none."""


class _Wire(BaseModel):
    """A wire object: fields a newer daemon adds are ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    @classmethod
    def read(cls, data: JsonValue) -> Self:
        """Return *data* as this object.

        Raises:
            UnreadableReplyError: *data* lacks a field this client needs.
        """
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            msg = (
                "biffd sent a talk reply this biff cannot read "
                f"({exc.error_count()} bad fields)"
            )
            raise UnreadableReplyError(msg) from exc


@final
class TalkStanding(_Wire):
    """Where a conversation stands: its phase, and with whom unless idle."""

    phase: Literal["idle", "inviting", "connected"]
    peer: str


@final
class WireMessage(_Wire):
    """One line, or the hangup, that reached the session."""

    kind: Literal["message", "end"]
    label: str
    body: str


@final
class WireInvite(_Wire):
    """Someone who wants to talk, and the command that accepts."""

    sender: str = Field(alias="from")
    tty: str
    label: str
    accept: str


@final
class TalkReading(_Wire):
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


@final
class TalkOutcome(_Wire):
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
