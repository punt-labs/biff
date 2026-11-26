"""The biffd frames: JSON objects a client and the daemon exchange.

Every frame carries a protocol ``version``.  Frames are plain data and cross
the connection as JSON; rendering is the client's job, so a reply holds
``wire`` data and never display text.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Literal, cast, final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)

from biff.session_routing import MAX_ID_LENGTH

__all__ = [
    "PROTOCOL_VERSION",
    "ClientType",
    "ErrorCode",
    "Event",
    "EventKind",
    "Frame",
    "FrameError",
    "Frames",
    "Hello",
    "Reply",
    "ReplyError",
    "Request",
    "Subscribe",
]

PROTOCOL_VERSION = 1

type ClientType = Literal["mcp", "cli", "hook", "plugin"]
type EventKind = Literal["unread", "mail", "wall"]

# The charset a session id has everywhere else (``session_routing.ID_RE``);
# anchored because a pattern constraint searches rather than matches.
type _SessionId = Annotated[
    str, StringConstraints(pattern=rf"^[A-Za-z0-9_-]{{1,{MAX_ID_LENGTH}}}$")
]
type _AgentId = Annotated[
    str, StringConstraints(pattern=rf"^[A-Za-z0-9_-]{{0,{MAX_ID_LENGTH}}}$")
]


class ErrorCode(StrEnum):
    """Why the daemon refused a frame."""

    UNSUPPORTED_VERSION = "unsupported_version"
    BAD_HELLO = "bad_hello"
    SESSION_CONFLICT = "session_conflict"
    SESSION_REPLACED = "session_replaced"
    UNKNOWN_METHOD = "unknown_method"
    INVALID_PARAMS = "invalid_params"
    INTERNAL = "internal"


class FrameError(ValueError):
    """A frame is not valid JSON, not an object, or not a known frame."""


class _Frame(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = PROTOCOL_VERSION


class Hello(_Frame):
    """First frame of a connection: the token and the caller's identity."""

    type: Literal["hello"] = "hello"
    token: str
    client: ClientType
    session_id: _SessionId
    # Empty when the harness states no main agent (a shell outside a harness).
    main_agent_id: _AgentId = ""
    # The directory the client runs in: it names the repository and relay.
    cwd: str
    # Overrides the configured identity, as ``--user`` does for the CLI.
    user: str = ""


class Request(_Frame):
    """One command invocation: ``method`` names a command object."""

    type: Literal["request"] = "request"
    id: int = Field(ge=1)
    method: str
    params: dict[str, JsonValue] = Field(default_factory=dict)


class ReplyError(BaseModel):
    """A refusal: a code to branch on and a message that says what to do."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ErrorCode
    message: str


class Reply(_Frame):
    """The answer to a request, or to the hello when ``id`` is 0."""

    type: Literal["reply"] = "reply"
    id: int = Field(ge=0)
    result: JsonValue = None
    error: ReplyError | None = None


class Subscribe(_Frame):
    """Start the arrival subscription for the connection's session."""

    type: Literal["subscribe"] = "subscribe"


class Event(_Frame):
    """One arrival summary; ``seq`` orders events across the daemon."""

    type: Literal["event"] = "event"
    seq: int = Field(ge=1)
    kind: EventKind
    data: JsonValue = None


type Frame = Annotated[
    Hello | Request | Reply | Subscribe | Event, Field(discriminator="type")
]

_ADAPTER: TypeAdapter[Frame] = TypeAdapter(Frame)
_TYPES = frozenset({"hello", "request", "reply", "subscribe", "event"})


@final
class Frames:
    """Encodes and decodes frames."""

    @staticmethod
    def encode(frame: Hello | Request | Reply | Subscribe | Event) -> str:
        """Return *frame* as a JSON object."""
        return frame.model_dump_json(exclude_none=False)

    @staticmethod
    def decode(text: str) -> Hello | Request | Reply | Subscribe | Event:
        """Return the frame *text* holds.

        Raises:
            FrameError: *text* is not JSON, not an object, lacks a ``version``,
                names no known frame type, or does not fit that frame.
        """
        try:
            raw: object = json.loads(text)
        except ValueError as exc:
            msg = "frame is not valid JSON"
            raise FrameError(msg) from exc
        if not isinstance(raw, dict):
            msg = "frame must be a JSON object"
            raise FrameError(msg)
        fields = cast("dict[str, object]", raw)
        if "version" not in fields:
            msg = "frame has no version"
            raise FrameError(msg)
        if fields.get("type") not in _TYPES:
            msg = f"unknown frame type {fields.get('type')!r}"
            raise FrameError(msg)
        try:
            return _ADAPTER.validate_python(fields)
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['type']}"
                for e in exc.errors()
            )
            msg = f"invalid {fields['type']} frame: {detail}"
            raise FrameError(msg) from exc
