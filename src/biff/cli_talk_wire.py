"""The objects biffd's talk replies are read into, read tolerantly."""

from __future__ import annotations

from typing import Literal, Self, final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from biff.cli_daemon_wire import UnreadableReplyError

__all__ = ["Wire", "WireInvite", "WireMessage"]


class Wire(BaseModel):
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
class WireMessage(Wire):
    """One line, or the hangup, that reached the session."""

    kind: Literal["message", "end"]
    label: str
    body: str


@final
class WireInvite(Wire):
    """Someone who wants to talk, and the command that accepts."""

    sender: str = Field(alias="from")
    tty: str
    label: str
    accept: str
