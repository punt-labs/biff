"""A request or hello the daemon refuses, with the reply that says why."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from biff.frame_reply import ErrorCode, ReplyError

__all__ = ["RefusalError"]


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
