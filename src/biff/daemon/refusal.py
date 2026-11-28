"""A frame or connection the daemon refuses, and what it tells the client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from biff.frame_reply import ErrorCode, ReplyError

__all__ = ["ClientGoneError", "HandshakeRefusedError", "RefusalError"]


# Not frozen: raising sets ``__traceback__``, which a frozen dataclass rejects.
@dataclass(eq=False)
class RefusalError(Exception):
    """The daemon refuses a frame; ``reply_error`` is what the client sees."""

    reply_error: ReplyError

    @classmethod
    def of(cls, code: ErrorCode, message: str) -> Self:
        """Return the refusal *code* with an actionable *message*."""
        return cls(ReplyError(code=code, message=message))

    def __str__(self) -> str:
        """Return the message the client sees."""
        return self.reply_error.message


@dataclass(eq=False)
class HandshakeRefusedError(Exception):
    """The handshake failed; the connection closes with *reason*, changing no state."""

    reason: str

    def __str__(self) -> str:
        """Return the reason."""
        return self.reason


class ClientGoneError(Exception):
    """An event could not be written because the client had already left.

    It is not a relay error: the arrival poll ends quietly, and nothing is
    announced to a connection that is gone.
    """
