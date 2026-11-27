"""How talk arrives on a terminal: the conversation lines, banners and hangup."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self, final

from biff._formatting import terminal_safe
from biff.formatting import format_talk_end, format_talk_line

if TYPE_CHECKING:
    from biff.cli_talk_reading import TalkReading
    from biff.cli_talk_wire import WireMessage
    from biff.repl_display import ReplDisplay

__all__ = ["TalkView"]

_YELLOW = "\033[1;33m"
_CYAN = "\033[36m"
_DIM = "\033[2m"
_RESET = "\033[0m"


@final
class TalkView:
    """Renders what ``talk_read`` returned in the who/read/wall ``▶`` idiom.

    A line in a live conversation is cyan; anything that reaches an idle
    prompt (an invite, a line from someone you are not talking to) is a yellow
    banner; a hangup is dim.  Every line honours the timestamp toggle.  An
    invite stays pending until answered, so each read lists it again: the view
    announces it once and forgets it when it is no longer pending.
    """

    __slots__ = ("_announced", "_display")

    _display: ReplDisplay
    _announced: set[str]

    def __new__(cls, display: ReplDisplay) -> Self:
        """Create a view that stamps lines as *display* says."""
        self = super().__new__(cls)
        self._display = display
        self._announced = set()
        return self

    def conversation(self, messages: tuple[WireMessage, ...]) -> list[str]:
        """Render the lines of a live conversation, and its hangup."""
        lines: list[str] = []
        for message in messages:
            if message.kind == "end":
                lines.append(f"{_DIM}{format_talk_end(message.label)}{_RESET}")
            elif message.body:
                lines.extend(self._lines(message, _CYAN))
        return lines

    def invites(self, reading: TalkReading) -> list[str]:
        """Render the invites not yet announced, one banner each."""
        self._announced &= {invite.accept for invite in reading.invites}
        lines: list[str] = []
        for invite in reading.invites:
            if invite.accept in self._announced:
                continue
            self._announced.add(invite.accept)
            body = f"wants to talk — {terminal_safe(invite.accept)} to accept"
            shown = format_talk_line(invite.label, body, stamp=self._stamp())
            lines.extend(f"{_YELLOW}{line}{_RESET}" for line in shown)
        return lines

    def banners(self, reading: TalkReading) -> list[str]:
        """Render what reached an idle prompt: new invites, and stray lines."""
        stray = [
            line
            for message in reading.messages
            if message.kind == "message" and message.body
            for line in self._lines(message, _YELLOW)
        ]
        return [*self.invites(reading), *stray]

    def _lines(self, message: WireMessage, colour: str) -> list[str]:
        shown = format_talk_line(message.label, message.body, stamp=self._stamp())
        return [f"{colour}{line}{_RESET}" for line in shown]

    def _stamp(self) -> str:
        return self._display.stamp(datetime.now(UTC))
