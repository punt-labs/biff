"""``biff talk`` runs as a client of biffd: invite, converse, hang up."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

from biff._formatting import HEADER_PREFIX
from biff.cli_daemon import DaemonCommand
from biff.cli_link import DaemonLink
from biff.cli_talk import TalkConversation
from biff.talk_reading import TalkReading
from tests.test_daemon.terminal import Screen, Typist

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from tests.test_daemon.conftest import Harness
    from tests.test_daemon.talk_bus import TalkBus


def _command(harness: Harness) -> DaemonCommand:
    return DaemonCommand(
        user_override="kai",
        session_ref=None,
        run_dir=harness.run_dir,
        nudge=lambda: harness.daemon.port,
    )


def _data(reply: object) -> dict[str, object]:
    return cast("dict[str, dict[str, object]]", reply)["data"]


async def test_biff_talk_invites_converses_and_hangs_up(
    harness: Harness, repo: Path, bus: TalkBus, capsys: pytest.CaptureFixture[str]
) -> None:
    assert bus is not None
    eric = await harness.connect("s-eric", "eric", cwd=str(repo))
    status = cast("dict[str, dict[str, str]]", await eric.request("status"))
    eric_at = f"eric:{status['data']['tty_name']}"
    typist, screen = Typist(), Screen(capsys)

    talk = asyncio.create_task(
        TalkConversation.alone(_command(harness), eric_at, "hello", read=typist)
    )
    await screen.shows(f"Waiting for {eric_at} to respond")

    # Eric sees the invite and accepts with a line of his own.
    async with asyncio.timeout(5.0):
        invites: list[dict[str, str]] = []
        while not invites:
            seen = _data(await eric.request("talk_read"))
            invites = cast("list[dict[str, str]]", seen["invites"])
            await asyncio.sleep(0.01)
    kai_at = f"kai:{invites[0]['tty']}"
    await eric.request("talk", to=kai_at, message="hi kai")
    await screen.shows(f"Connected to {eric_at}")
    await screen.shows("hi kai")
    await screen.shows("you> hello")  # echoed once it was sent, after the accept

    typist.type("what's up")
    async with asyncio.timeout(5.0):
        lines: list[str] = []
        while len(lines) < 2:
            seen = _data(await eric.request("talk_read"))
            lines += cast("list[str]", seen["lines"])
            await asyncio.sleep(0.01)
    assert lines == [
        f"{HEADER_PREFIX}{kai_at}: hello",  # the opening line, after the accept
        f"{HEADER_PREFIX}{kai_at}: what's up",
    ]

    typist.type("end")
    assert await asyncio.wait_for(talk, timeout=5.0) is True
    await screen.shows(f"Talk with {eric_at} ended.")
    ended = _data(await eric.request("talk_read"))
    assert ended["ended"] is True


async def test_biff_talk_is_refused_on_a_relay_that_carries_no_talk(
    harness: Harness, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    typist, screen = Typist(), Screen(capsys)
    talked = await TalkConversation.alone(
        _command(harness), "eric:tty1", "", read=typist
    )
    await screen.shows("Talk requires a NATS relay connection.")
    assert talked is False


async def test_biff_talk_to_someone_offline_says_so_and_fails(
    harness: Harness, repo: Path, bus: TalkBus, capsys: pytest.CaptureFixture[str]
) -> None:
    assert bus is not None
    typist, screen = Typist(), Screen(capsys)
    talked = await TalkConversation.alone(
        _command(harness), "nobody:tty1", "", read=typist
    )
    await screen.shows("nobody is not online.")
    assert talked is False


async def test_a_warning_is_shown_when_the_owed_accept_never_reached_the_partner(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A mutual-invite auto-accept that could not be sent must not look connected."""
    link = MagicMock()
    link.pump.lost = ""
    link.talk_read = AsyncMock(
        return_value=TalkReading(
            lines=(),
            messages=(),
            invites=(),
            ended=False,
            accept_published=False,
            opening="",
            opening_unsent=False,
            dropped=0,
            phase="connected",
            peer="eric:tty2",
        )
    )
    console = MagicMock()
    shown: list[str] = []
    console.say = lambda text="", *, end="\n": shown.append(text)
    conversation = TalkConversation(
        link=cast("DaemonLink", link),
        console=console,
        view=MagicMock(invites=MagicMock(return_value=[])),
        prompt="you> ",
    )

    connected = await conversation._accepted("eric:tty2")

    assert connected is True  # locally connected: proceed, but say so
    assert any("may not have" in line for line in shown)


async def test_a_plan_that_cannot_be_set_is_logged_and_never_ends_the_talk(
    harness: Harness, repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    link = await DaemonLink.open(_command(harness))
    await harness.daemon.stop()
    with caplog.at_level(logging.DEBUG, logger="biff.cli_link"):
        await link.set_plan("talking to eric:tty2")  # the connection is gone
    await link.close()
    assert [r.exc_info is not None for r in caplog.records] == [True]


async def test_biff_talk_fails_when_biffd_is_lost_mid_talk(
    harness: Harness, repo: Path, bus: TalkBus, capsys: pytest.CaptureFixture[str]
) -> None:
    assert bus is not None
    eric = await harness.connect("s-eric", "eric", cwd=str(repo))
    status = cast("dict[str, dict[str, str]]", await eric.request("status"))
    eric_at = f"eric:{status['data']['tty_name']}"
    typist, screen = Typist(), Screen(capsys)
    talk = asyncio.create_task(
        TalkConversation.alone(_command(harness), eric_at, "", read=typist)
    )
    await screen.shows("Waiting for")
    await harness.daemon.stop()
    assert await asyncio.wait_for(talk, timeout=5.0) is False
    await screen.shows("biffd connection lost")


async def test_a_hangup_that_arrives_with_the_accept_ends_the_wait(
    capsys: pytest.CaptureFixture[str],
) -> None:
    link = MagicMock()
    link.pump.lost = ""
    link.talk_read = AsyncMock(
        return_value=TalkReading(
            lines=(),
            messages=(),
            invites=(),
            ended=True,  # the accept and the end were drained together
            accept_published=True,
            opening="",
            opening_unsent=False,
            dropped=0,
            phase="idle",
            peer="",
        )
    )
    console = MagicMock()
    shown: list[str] = []
    console.say = lambda text="", *, end="\n": shown.append(text)
    conversation = TalkConversation(
        link=cast("DaemonLink", link),
        console=console,
        view=MagicMock(
            invites=MagicMock(return_value=[]), dropped=MagicMock(return_value=[])
        ),
        prompt="you> ",
    )

    connected = await conversation._accepted("eric:tty2")

    assert connected is False
    assert any("ended" in line for line in shown)
