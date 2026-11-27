"""The daemon's answer to a request: a result or a refusal."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from biff.frame_base import FrameBase

__all__ = ["ErrorCode", "Reply", "ReplyError"]


class ErrorCode(StrEnum):
    """Why the daemon refused a frame."""

    UNSUPPORTED_VERSION = "unsupported_version"
    BAD_HELLO = "bad_hello"
    SESSION_CONFLICT = "session_conflict"
    SESSION_REPLACED = "session_replaced"
    SESSION_ENDED = "session_ended"
    UNKNOWN_METHOD = "unknown_method"
    INVALID_PARAMS = "invalid_params"
    ALREADY_SUBSCRIBED = "already_subscribed"
    INTERNAL = "internal"


class ReplyError(BaseModel):
    """A refusal: a code to branch on and a message that says what to do."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ErrorCode
    message: str


class Reply(FrameBase):
    """The answer to a request, or to the hello when ``id`` is 0."""

    type: Literal["reply"] = "reply"
    id: int = Field(ge=0)
    result: JsonValue = None
    error: ReplyError | None = None
    # The reply carries mail that stays unread until the client sends an ack.
    needs_ack: bool = False
