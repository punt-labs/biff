"""The protocol version and the base every biffd frame shares."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = ["MAX_FRAME_BYTES", "PROTOCOL_VERSION", "FrameBase"]

PROTOCOL_VERSION = 2
"""The wire version; a peer of another version is refused at the handshake.

Version 2 added the ``channel_check_request`` event.  A version-1 client rejects
an event kind it does not know and drops the connection mid-stream, so it must
be told to upgrade at the hello instead.
"""

MAX_FRAME_BYTES = 1_048_576
"""The largest frame either side accepts, in bytes of UTF-8 JSON."""


class FrameBase(BaseModel):
    """A frame: a strict, immutable JSON object that carries a protocol version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = PROTOCOL_VERSION
