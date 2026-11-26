"""The protocol version and the base every biffd frame shares."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = ["PROTOCOL_VERSION", "FrameBase"]

PROTOCOL_VERSION = 1


class FrameBase(BaseModel):
    """A frame: a strict, immutable JSON object that carries a protocol version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = PROTOCOL_VERSION
