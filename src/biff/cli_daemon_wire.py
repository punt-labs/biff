"""What biffd answers a command with, read tolerantly and refused clearly."""

from __future__ import annotations

from typing import Self, final

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from biff.commands import CommandResult

__all__ = ["UnreadableReplyError", "WireResult"]

_OLDER = (
    "biffd is older than this biff, or sent a reply this biff cannot read; "
    "run: biff daemon restart"
)


class UnreadableReplyError(ConnectionError):
    """biffd answered with something this client cannot use."""


@final
class WireResult(BaseModel):
    """The ``{data, error, text, more}`` result of one command.

    Fields a newer daemon adds are ignored, so a client keeps working across an
    upgrade of biffd; a reply missing a field this client needs is refused.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    data: JsonValue
    error: bool
    text: str
    more: int = 0

    @classmethod
    def parse(cls, raw: JsonValue) -> Self:
        """Return *raw* as a result.

        Raises:
            UnreadableReplyError: *raw* lacks a field every reply has, as an
                older daemon's does.
        """
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise UnreadableReplyError(_OLDER) from exc

    def command_result(self) -> CommandResult:
        """Return what the command prints, saying when mail is left to read."""
        if self.more <= 0:
            return CommandResult(text=self.text, json_data=self.data, error=self.error)
        return CommandResult(
            text=f"{self.text}\n{self.more} more unread; run biff read again",
            json_data={"messages": self.data, "more": self.more},
            error=self.error,
        )
