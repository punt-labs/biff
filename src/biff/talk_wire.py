"""The talk objects biffd sends and a client reads: one definition for both ends."""

from __future__ import annotations

from typing import Literal, final

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Wire", "WireInvite", "WireMessage"]


class Wire(BaseModel):
    """A wire object: fields a newer daemon adds are ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


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
