"""A connection lost inside a request ends a talk as cleanly as one lost between them.

The daemon can die while the loop waits on the event pump (the pump records it)
or while a request is in flight (the request raises).  Both must end the talk
with the same text and the same exit status, and neither may escape as a
traceback.  Every awaited link call is exercised with a fake that raises.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from biff.cli_console import Tick
from biff.cli_daemon import DaemonCommand
from biff.cli_repl import ReplSession
from biff.cli_talk import TalkConversation
from biff.cli_talk_outcome import TalkOutcome
from biff.cli_talk_view import TalkView
from biff.client.connection import ConnectionLostError
from biff.commands import CommandResult
from biff.dispatch import Call
from biff.repl_display import ReplDisplay
from biff.talk_reading import TalkReading

if TYPE_CHECKING:
    from biff.cli_link import DaemonLink

LOST = ConnectionLostError("biffd closed the connection (code 1001)")


def _reading(phase: str, *, ended: bool = False) -> TalkReading:
    return TalkReading.model_validate(
        {
            "lines": [],
            "messages": [],
            "invites": [],
            "ended": ended,
            "accept_published": True,
            "opening": "",
            "opening_unsent": False,
            "dropped": 0,
            "ready": True,
            "phase": phase,
            "peer": "eric:tty2",
        }
    )


def _outcome(phase: str) -> TalkOutcome:
    return TalkOutcome(text="ok", error=False, phase=phase, peer="eric:tty2")  # type: ignore[arg-type]


class _Link:
    """A link whose calls succeed, except the ones a test names."""

    def __init__(self, *, phase: str, raises: set[str]) -> None:
        self.pump = MagicMock(lost="", wake=MagicMock())
        self.raises = raises
        self._phase = phase

    async def _go(self, name: str) -> None:
        if name in self.raises:
            raise LOST

    async def talk(self, to: str, message: str) -> TalkOutcome:
        await self._go("talk")
        return _outcome(self._phase)

    async def talk_read(self) -> TalkReading:
        await self._go("talk_read")
        return _reading("inviting")

    async def talk_end(self) -> CommandResult:
        await self._go("talk_end")
        return CommandResult(text="ended")

    async def set_plan(self, text: str) -> None:
        await self._go("set_plan")

    async def status(self) -> dict[str, object]:
        await self._go("status")
        return {"unread": 0, "wall": None}


def _console(lines: list[object]) -> MagicMock:
    console = MagicMock()
    console.prompt = "you> "
    console.say = MagicMock()
    console.complain = MagicMock()
    console.next = AsyncMock(side_effect=[*lines, None])
    return console


def _talk(link: _Link, console: MagicMock) -> TalkConversation:
    return TalkConversation(
        link=cast("DaemonLink", link),
        console=console,
        view=TalkView(ReplDisplay()),
        prompt="you> ",
    )


def _complained(console: MagicMock) -> str:
    return " ".join(str(c.args[0]) for c in console.complain.call_args_list)


@pytest.mark.parametrize(
    ("phase", "raises", "typed"),
    [
        ("inviting", {"talk"}, []),  # the first request
        ("inviting", {"talk_read"}, [Tick.IDLE]),  # reading the accept
        ("connected", {"talk_read"}, [Tick.IDLE]),  # reading an arrival
        ("connected", {"talk"}, ["a line"]),  # sending a line
        ("connected", {"set_plan"}, []),  # the presence line
    ],
)
async def test_a_request_lost_in_flight_ends_the_talk_as_lost(
    phase: str, raises: set[str], typed: list[object]
) -> None:
    link = _Link(phase=phase, raises=raises)
    console = _console(typed)
    talked = await _talk(link, console).run("eric:tty2", "")
    assert talked is False  # biff talk exits 1
    assert "biffd connection lost" in _complained(console)


async def test_a_hangup_lost_in_flight_still_ends_the_talk() -> None:
    link = _Link(phase="connected", raises={"talk_end"})
    console = _console(["end"])
    assert await _talk(link, console).run("eric:tty2", "") is True  # ended by the user


async def test_the_repl_prints_a_lost_request_and_keeps_its_prompt() -> None:
    link = _Link(phase="connected", raises={"status"})
    command = MagicMock()
    answer = MagicMock()
    answer.deliver = AsyncMock()
    command.call = AsyncMock(return_value=answer)
    console = _console([])
    session = ReplSession(
        command=cast("DaemonCommand", command),
        link=cast("DaemonLink", link),
        console=console,
    )
    await session._guarded(session._command(Call("who", {})))
    assert "connection lost" in _complained(console)


async def test_the_repl_prints_a_lost_command_and_keeps_its_prompt() -> None:
    link = _Link(phase="connected", raises=set())
    command = MagicMock()
    command.call = AsyncMock(side_effect=LOST)
    console = _console([])
    session = ReplSession(
        command=cast("DaemonCommand", command),
        link=cast("DaemonLink", link),
        console=console,
    )
    await session._guarded(session._command(Call("who", {})))
    assert "connection lost" in _complained(console)
