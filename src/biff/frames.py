"""The biffd frames: JSON objects a client and the daemon exchange.

Every frame carries a protocol ``version``.  Frames are plain data and cross
the connection as JSON; rendering is the client's job, so a reply holds
``wire`` data and never display text.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal, cast, final

from pydantic import (
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
)

from biff.frame_base import FrameBase
from biff.frame_client import Hello, Request, Subscribe
from biff.frame_proof import Challenge, Proof
from biff.frame_reply import Reply

__all__ = ["Event", "EventKind", "Frame", "FrameError", "Frames"]

type EventKind = Literal["unread", "mail", "wall"]


class FrameError(ValueError):
    """A frame is not valid JSON, not an object, or not a known frame."""


class Event(FrameBase):
    """One arrival summary; ``seq`` orders events across the daemon."""

    type: Literal["event"] = "event"
    seq: int = Field(ge=1)
    kind: EventKind
    data: JsonValue = None


type Frame = Annotated[
    Challenge | Proof | Hello | Request | Reply | Subscribe | Event,
    Field(discriminator="type"),
]

_ADAPTER: TypeAdapter[Frame] = TypeAdapter(Frame)
_TYPES = frozenset(
    {"challenge", "proof", "hello", "request", "reply", "subscribe", "event"}
)


@final
class Frames:
    """Encodes and decodes frames."""

    @staticmethod
    def encode(
        frame: Challenge | Proof | Hello | Request | Reply | Subscribe | Event,
    ) -> str:
        """Return *frame* as a JSON object."""
        return frame.model_dump_json(exclude_none=False)

    @staticmethod
    def decode(
        text: str,
    ) -> Challenge | Proof | Hello | Request | Reply | Subscribe | Event:
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
