"""A connection lost inside a request ends a talk as cleanly as one lost between them.

The daemon can die while the loop waits on the event pump (the pump records it)
or while a request is in flight (the request raises).  Both must end the talk
with the same text and the same exit status, and neither may escape as a
traceback.  Every awaited link call is exercised with a fake that raises.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from biff.cli_console import Tick
from biff.cli_daemon import DaemonCommand
from biff.cli_link import DaemonLink
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
    from biff.client.connection import DaemonClient

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
        self._talks = 0

    async def _go(self, name: str) -> None:
        if name in self.raises:
            raise LOST

    async def talk(self, to: str, message: str) -> TalkOutcome:
        # The first call opens the talk ("talk"); each later call sends a typed
        # line ("send"), so a test can lose the connection at either.
        self._talks += 1
        await self._go("talk" if self._talks == 1 else "send")
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
        ("connected", {"send"}, ["a line"]),  # the typed line, after a good open
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


class _Client:
    """A connection under a real :class:`DaemonLink`: it replies or is lost."""

    def __init__(self, raises: set[str]) -> None:
        self.raises = raises
        self.asked: list[str] = []

    async def request(self, method: str, **params: object) -> object:
        self.asked.append(method)
        if method in self.raises:
            raise LOST
        replies: dict[str, object] = {
            "talk": {
                "data": {"phase": "connected", "peer": "eric:tty2"},
                "error": False,
                "text": "ok",
            },
            "talk_end": {"data": {"ended": True}, "error": False, "text": "ended"},
            "plan": {"data": None, "error": False, "text": ""},
        }
        return replies[method]


async def test_a_plan_the_real_link_cannot_set_never_ends_the_talk() -> None:
    """The link swallows a lost plan write, so the talk runs to its own end."""
    client = _Client({"plan"})
    console = _console(["end"])
    talk = TalkConversation(
        link=DaemonLink(cast("DaemonClient", client)),
        console=console,
        view=TalkView(ReplDisplay()),
        prompt="you> ",
    )
    assert await talk.run("eric:tty2", "") is True
    assert "plan" in client.asked  # it was tried
    assert _complained(console) == ""  # and the loss of it is not reported as a loss


async def test_a_hangup_biffd_never_acknowledges_is_logged_and_not_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A lost hang-up leaves the exit status alone: biffd resets its side anyway."""
    link = _Link(phase="connected", raises={"talk_end"})
    console = _console(["end"])
    with caplog.at_level(logging.WARNING, logger="biff.cli_talk"):
        talked = await _talk(link, console).run("eric:tty2", "")
    assert talked is True
    assert "could not tell biffd the talk ended" in caplog.text
    assert _complained(console) == ""  # nothing is reported as a loss


async def test_a_hangup_biffd_could_not_deliver_is_shown_and_not_a_failure() -> None:
    link = _Link(phase="connected", raises=set())
    link.talk_end = AsyncMock(  # type: ignore[method-assign]
        return_value=CommandResult(
            text="ended locally; the peer may not know", error=True
        )
    )
    console = _console(["end"])
    assert await _talk(link, console).run("eric:tty2", "") is True
    said = " ".join(str(c.args[0]) for c in console.say.call_args_list)
    assert "ended locally; the peer may not know" in said


async def test_the_shell_exits_when_the_standing_connection_records_a_loss() -> None:
    """The prompt loop itself ends when the pump says biffd is gone."""
    link = _Link(phase="connected", raises=set())
    link.pump = MagicMock(wake=MagicMock())
    link.pump.lost = ""
    console = _console([])

    async def quiet(wake: object) -> Tick:
        await asyncio.sleep(0)  # a real console yields; a stuck loop must time out
        link.pump.lost = "biffd closed the connection (code 1001)"  # lost meanwhile
        return Tick.IDLE

    console.next = AsyncMock(side_effect=quiet)
    session = ReplSession(
        command=cast("DaemonCommand", MagicMock()),
        link=cast("DaemonLink", link),
        console=console,
    )
    await asyncio.wait_for(session.run({"unread": 0, "wall": None}), timeout=1.0)
    said = " ".join(str(c.args[0]) for c in console.say.call_args_list)
    assert "biffd connection lost" in said
