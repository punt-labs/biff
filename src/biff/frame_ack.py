"""The frame a client sends to confirm it received a reply."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from biff.frame_base import FrameBase

__all__ = ["Ack"]


class Ack(FrameBase):
    """The client received the reply to request ``id``.

    The daemon asks for it (``Reply.needs_ack``) when the reply carries mail:
    the mail is marked read only on this frame, so a reply the client gave up on
    or never got leaves its mail unread.
    """

    type: Literal["ack"] = "ack"
    id: int = Field(ge=1)
