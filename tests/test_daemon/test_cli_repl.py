"""The REPL runs as a client of biffd: scripted stdin against a real daemon."""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, cast

import pytest

from biff._formatting import HEADER_PREFIX
from biff.cli_daemon import Answer, DaemonCommand
from biff.cli_link import DaemonLink
from biff.cli_repl import ReplSession
from biff.client.connection import DaemonError
from biff.frame_reply import ErrorCode
from biff.talk_reading import TalkReading
from tests.test_daemon.terminal import Screen, Typist, eventually

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from biff.client.connection import DaemonClient
    from tests.test_daemon.conftest import Harness
    from tests.test_daemon.talk_bus import TalkBus


class Session:
    """A REPL running against the harness daemon, and the screen it draws on."""

    def __init__(
        self, harness: Harness, typist: Typist, screen: Screen, task: asyncio.Task[None]
    ) -> None:
        self.harness = harness
        self.typist = typist
        self.screen = screen
        self.task = task

    async def run(self, line: str, *, shows: str) -> None:
        """Type *line* and wait for *shows* on the screen."""
        self.typist.type(line)
        await self.screen.shows(shows)

    async def ends(self) -> None:
        """Wait for the REPL to return."""
        async with asyncio.timeout(5.0):
            await self.task


def _command(harness: Harness) -> DaemonCommand:
    return DaemonCommand(
        user_override="kai",
        session_ref=None,
        run_dir=harness.run_dir,
        nudge=lambda: harness.daemon.port,
    )


@pytest.fixture
async def repl(
    harness: Harness, repo: Path, capsys: pytest.CaptureFixture[str]
) -> AsyncIterator[Session]:
    typist = Typist()
    task = asyncio.create_task(ReplSession.interactive(_command(harness), read=typist))
    session = Session(harness, typist, Screen(capsys), task)
    await typist.prompted(1)
    yield session
    typist.end_input()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _eric(harness: Harness, repo: Path) -> DaemonClient:
    return await harness.connect("s-eric", "eric", cwd=str(repo))


async def _tty(client: DaemonClient) -> str:
    status = cast("dict[str, dict[str, str]]", await client.request("status"))
    return status["data"]["tty_name"]


def _data(reply: object) -> dict[str, object]:
    return cast("dict[str, dict[str, object]]", reply)["data"]


async def test_the_banner_names_the_session_and_the_commands(repl: Session) -> None:
    await repl.screen.shows("kai:tty1")
    await repl.screen.shows("Commands: ")


async def test_a_command_prints_the_text_biffd_rendered(repl: Session) -> None:
    await repl.run("who", shows="kai")


async def test_the_next_prompt_waits_for_the_output(repl: Session) -> None:
    repl.typist.type("who")
    await repl.typist.prompted(2)
    repl.screen.refresh()
    assert "kai" in repl.screen.out  # the table was written before the prompt


async def test_exit_ends_the_repl(repl: Session) -> None:
    repl.typist.type("exit")
    await repl.ends()


async def test_end_of_input_ends_the_repl(repl: Session) -> None:
    repl.typist.end_input()
    await repl.ends()


async def test_an_unknown_command_names_the_commands(repl: Session) -> None:
    await repl.run("frobnicate", shows="Unknown command: frobnicate. Available:")


async def test_a_misused_command_prints_its_usage(repl: Session) -> None:
    await repl.run("finger", shows="Usage: finger user")


async def test_a_blank_line_prints_nothing_and_prompts_again(repl: Session) -> None:
    repl.typist.type("")
    await repl.typist.prompted(2)
    repl.screen.refresh()
    assert "Unknown" not in repl.screen.out


async def test_timestamps_can_be_turned_on_and_off(repl: Session) -> None:
    await repl.run("timestamps on", shows="Timestamps on.")
    await repl.run("timestamps off", shows="Timestamps off.")


async def test_timestamps_without_an_argument_prints_usage(repl: Session) -> None:
    await repl.run("timestamps", shows="Usage: timestamps on|off")


async def test_a_plan_set_at_the_prompt_is_seen_by_others(
    repl: Session, harness: Harness, repo: Path
) -> None:
    await repl.run("plan ship biffd", shows="ship biffd")
    eric = await _eric(harness, repo)
    rows = cast("list[dict[str, str]]", _data(await eric.request("who")))
    assert {row["user"]: row["plan"] for row in rows}["kai"] == "ship biffd"


async def test_mail_from_another_session_is_announced(
    repl: Session, harness: Harness, repo: Path
) -> None:
    eric = await _eric(harness, repo)
    await eric.request("write", to="kai", message="secret body")
    await repl.screen.shows("1 new message")
    assert "secret body" not in repl.screen.out


async def test_a_wall_from_another_session_is_announced(
    repl: Session, harness: Harness, repo: Path
) -> None:
    eric = await _eric(harness, repo)
    await eric.request("wall", message="deploy freeze", duration="1h")
    await repl.screen.shows("deploy freeze")


async def test_a_wall_posted_at_the_prompt_is_not_announced_back(
    repl: Session,
) -> None:
    await repl.run("wall mine 1h", shows="mine")
    repl.screen.refresh()
    before = repl.screen.out.count("mine")
    await asyncio.sleep(0.3)
    repl.screen.refresh()
    assert repl.screen.out.count("mine") == before


async def test_reading_marks_mail_read_so_it_is_not_announced_twice(
    repl: Session, harness: Harness, repo: Path
) -> None:
    eric = await _eric(harness, repo)
    await eric.request("write", to="kai", message="once")
    await repl.screen.shows("1 new message")
    await repl.run("read", shows="once")
    await repl.screen.stays_without("2 new messages")


async def test_a_lost_connection_ends_the_repl(repl: Session, harness: Harness) -> None:
    await harness.daemon.stop()
    await repl.ends()
    repl.screen.refresh()
    assert "biffd connection lost" in repl.screen.out


# --- talk ---------------------------------------------------------------


async def test_talk_needs_a_relay_that_carries_it(repl: Session) -> None:
    await repl.run("talk eric:tty1", shows="Talk requires a NATS relay connection.")


async def test_talk_with_no_address_prints_usage(repl: Session) -> None:
    await repl.run("talk", shows="Usage: talk user:ttyN [message]")


@pytest.fixture
async def talkers(bus: TalkBus, repl: Session, harness: Harness, repo: Path) -> Talkers:
    """The REPL (kai) on a relay that carries talk, and eric's client."""
    assert bus is not None
    eric = await _eric(harness, repo)
    return Talkers(
        repl=repl,
        bus=bus,
        eric=eric,
        eric_at=f"eric:{await _tty(eric)}",
        kai_at=repl.typist.prompts[0].removesuffix(" ▶ "),
    )


class Talkers:
    """The REPL's user, kai, and eric's client, who each know the other's address."""

    def __init__(
        self,
        *,
        repl: Session,
        bus: TalkBus,
        eric: DaemonClient,
        eric_at: str,
        kai_at: str,
    ) -> None:
        self.repl = repl
        self.bus = bus
        self.eric = eric
        self.eric_at = eric_at
        self.kai_at = kai_at

    async def connect(self, opening: str = "") -> None:
        """Kai invites eric from the REPL; eric accepts with a line."""
        self.repl.typist.type(f"talk {self.eric_at} {opening}".rstrip())
        await self.repl.screen.shows(f"Waiting for {self.eric_at} to respond")
        await eventually_invited(self.eric)
        await self.eric.request("talk", to=self.kai_at, message="hi kai")
        await self.repl.screen.shows(f"Connected to {self.eric_at}")

    async def lines_at_eric(self, count: int) -> list[str]:
        """Collect the lines eric receives until there are *count*."""
        got: list[str] = []

        def enough() -> bool:
            return len(got) >= count

        async with asyncio.timeout(5.0):
            while not enough():
                seen = _data(await self.eric.request("talk_read"))
                got.extend(cast("list[str]", seen["lines"]))
                await asyncio.sleep(0.01)
        return got


async def eventually_invited(client: DaemonClient) -> None:
    """Wait until *client*'s session holds an invite, without draining a line."""
    async with asyncio.timeout(5.0):
        while True:
            seen = _data(await client.request("talk_read"))
            if seen["invites"]:
                return
            await asyncio.sleep(0.01)


async def test_an_invited_peer_who_accepts_joins_the_conversation(
    talkers: Talkers,
) -> None:
    await talkers.connect("hello eric")
    await talkers.repl.screen.shows("hi kai")  # eric's opening line, after Connected


async def test_lines_typed_in_a_talk_reach_the_peer_in_order(talkers: Talkers) -> None:
    await talkers.connect()
    for n in (1, 2, 3):
        talkers.repl.typist.type(f"kai says {n}")
    assert await talkers.lines_at_eric(3) == [
        f"{HEADER_PREFIX}{talkers.kai_at}: kai says {n}" for n in (1, 2, 3)
    ]


async def test_lines_the_peer_sends_are_shown_in_the_conversation(
    talkers: Talkers,
) -> None:
    await talkers.connect()
    for n in (1, 2, 3):
        await talkers.eric.request("talk", to=talkers.kai_at, message=f"eric says {n}")
    for n in (1, 2, 3):
        await talkers.repl.screen.shows(f"eric says {n}")


async def test_typing_end_hangs_up_and_returns_to_the_prompt(talkers: Talkers) -> None:
    await talkers.connect()
    talkers.repl.typist.type("end")
    await talkers.repl.screen.shows(f"Talk with {talkers.eric_at} ended.")
    seen = _data(await talkers.eric.request("talk_read"))
    assert seen["ended"] is True
    await talkers.repl.run("who", shows="eric:tty1")  # the REPL is back at its prompt


async def test_the_peer_hanging_up_returns_the_repl_to_the_prompt(
    talkers: Talkers,
) -> None:
    await talkers.connect()
    await talkers.eric.request("talk_end")
    await talkers.repl.screen.shows(f"Talk with {talkers.eric_at} ended.")
    await talkers.repl.run("who", shows="eric:tty1")


async def test_cancelling_an_invite_withdraws_it(talkers: Talkers) -> None:
    talkers.repl.typist.type(f"talk {talkers.eric_at}")
    await talkers.repl.screen.shows("Waiting for")
    await eventually_invited(talkers.eric)
    talkers.repl.typist.type("end")
    await talkers.repl.screen.shows(f"Talk with {talkers.eric_at} cancelled.")
    await eventually(lambda: _withdrawn(talkers.bus))


def _withdrawn(bus: TalkBus) -> bool:
    return any(b'"type": "withdraw"' in payload for _, payload in bus.published)


async def test_ctrl_c_while_inviting_withdraws_the_invite(talkers: Talkers) -> None:
    talkers.repl.typist.type(f"talk {talkers.eric_at}")
    await talkers.repl.screen.shows("Waiting for")
    await eventually_invited(talkers.eric)
    talkers.repl.task.cancel()
    await asyncio.gather(talkers.repl.task, return_exceptions=True)
    await eventually(lambda: _withdrawn(talkers.bus))


async def test_an_invite_at_an_idle_prompt_is_announced_and_can_be_accepted(
    talkers: Talkers,
) -> None:
    await talkers.eric.request("talk", to=talkers.kai_at, message="got a minute")
    await talkers.repl.screen.shows(f"wants to talk — talk {talkers.eric_at} to accept")
    talkers.repl.typist.type(f"talk {talkers.eric_at}")
    await talkers.repl.screen.shows(f"Connected to {talkers.eric_at}")


async def test_a_line_from_a_stranger_at_an_idle_prompt_is_announced(
    talkers: Talkers,
) -> None:
    await talkers.eric.request("talk", to=talkers.kai_at, message="")
    await talkers.repl.screen.shows("wants to talk")
    kai_subject = talkers.bus.published[-1][0]  # where the invite went
    status = cast("dict[str, dict[str, str]]", await talkers.eric.request("status"))
    stray = {
        "type": "message",
        "from": "eric",
        "from_tty": status["data"]["tty_name"],
        "from_key": status["data"]["session_key"],
        "to_key": kai_subject.removeprefix("talk.notify."),
        "body": "stray line",
    }
    await talkers.bus.publish(kai_subject, json.dumps(stray).encode())
    await talkers.repl.screen.shows("stray line")


async def test_a_line_that_cannot_be_sent_is_reported_and_the_talk_goes_on(
    talkers: Talkers,
) -> None:
    await talkers.connect()
    talkers.bus.broken = True
    talkers.repl.typist.type("lost line")
    await talkers.repl.screen.shows("message not sent")
    talkers.bus.broken = False
    talkers.repl.typist.type("kept line")
    assert await talkers.lines_at_eric(1) == [
        f"{HEADER_PREFIX}{talkers.kai_at}: kept line"
    ]


async def test_talk_the_daemon_held_before_the_repl_attached_is_shown_at_start(
    harness: Harness,
    repo: Path,
    bus: TalkBus,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert bus is not None
    command = _command(harness)
    holder = await DaemonLink.open(command)  # keeps kai's session attached
    status = await holder.status()
    eric = await _eric(harness, repo)
    await eric.request("talk", to=f"kai:{status['tty_name']}", message="")
    typist, screen = Typist(), Screen(capsys)
    task = asyncio.create_task(ReplSession.interactive(command, read=typist))
    await screen.shows("wants to talk")
    typist.end_input()
    await asyncio.gather(task, return_exceptions=True)
    await holder.close()


async def test_a_refused_request_is_a_message_and_the_repl_goes_on(
    repl: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*_args: object, **_kwargs: object) -> Answer:
        raise DaemonError(ErrorCode.INTERNAL, "who failed in biffd")

    monkeypatch.setattr(DaemonCommand, "call", refuse)
    repl.typist.type("who")
    await eventually(lambda: "who failed in biffd" in _err(repl))
    monkeypatch.undo()
    await repl.run("status", shows="unread:")


def _err(repl: Session) -> str:
    repl.screen.refresh()
    return repl.screen.err


async def _plans(client: DaemonClient) -> dict[str, str]:
    rows = cast("list[dict[str, str]]", _data(await client.request("who")))
    return {row["user"]: row["plan"] for row in rows}


async def test_timestamps_stamp_the_lines_of_a_conversation(talkers: Talkers) -> None:
    await talkers.repl.run("timestamps on", shows="Timestamps on.")
    await talkers.connect()
    await talkers.eric.request("talk", to=talkers.kai_at, message="stamped")
    await talkers.repl.screen.shows("stamped")
    assert re.search(r"\[\d{2}:\d{2}\] eric:tty1  stamped", talkers.repl.screen.out)


async def test_a_failed_invite_is_reported_and_leaves_no_talking_plan(
    talkers: Talkers,
) -> None:
    talkers.bus.broken = True
    await talkers.repl.run(f"talk {talkers.eric_at}", shows="invite not sent")
    talkers.bus.broken = False
    assert "talking to" not in (await _plans(talkers.eric))["kai"]


async def test_the_plan_names_the_peer_while_talking_and_clears_after(
    talkers: Talkers,
) -> None:
    await talkers.connect()
    assert (await _plans(talkers.eric))["kai"] == f"talking to {talkers.eric_at}"
    talkers.repl.typist.type("end")
    await talkers.repl.screen.shows("ended.")
    async with asyncio.timeout(5.0):
        while (await _plans(talkers.eric))["kai"] != "":
            await asyncio.sleep(0.01)


async def test_a_cancelled_invite_whose_withdraw_is_lost_says_so(
    talkers: Talkers,
) -> None:
    talkers.repl.typist.type(f"talk {talkers.eric_at}")
    await talkers.repl.screen.shows("Waiting for")
    talkers.bus.broken = True
    talkers.repl.typist.type("end")
    await talkers.repl.screen.shows("withdrawn locally")


async def test_a_hangup_that_cannot_be_sent_says_so(talkers: Talkers) -> None:
    await talkers.connect()
    talkers.bus.broken = True
    talkers.repl.typist.type("end")
    await talkers.repl.screen.shows("ended locally")


async def test_a_failed_talk_read_is_retried_so_the_invite_is_still_announced(
    talkers: Talkers, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = DaemonLink.talk_read
    failures = [OSError("the connection hiccupped")]

    async def flaky(self: DaemonLink) -> TalkReading:
        if failures:
            raise failures.pop()
        return await real(self)

    monkeypatch.setattr(DaemonLink, "talk_read", flaky)
    await talkers.eric.request("talk", to=talkers.kai_at, message="")
    await talkers.repl.screen.shows("wants to talk")


async def test_the_startup_read_is_retried_until_the_subscription_is_confirmed(
    harness: Harness,
    repo: Path,
    bus: TalkBus,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _command(harness)
    holder = await DaemonLink.open(command)  # keeps kai's session attached
    status = await holder.status()
    eric = await _eric(harness, repo)
    await eric.request("talk", to=f"kai:{status['tty_name']}", message="")
    bus.flush_gate = asyncio.Event()
    before = bus.total_subscribers()
    bus.relays[0].generation += 1  # kai resubscribes; the server has not confirmed
    await eventually(lambda: bus.total_subscribers() < before)
    readiness: list[bool] = []
    real = DaemonLink.talk_read

    async def watched(self: DaemonLink) -> TalkReading:
        reading = await real(self)
        readiness.append(reading.ready)
        return reading

    monkeypatch.setattr(DaemonLink, "talk_read", watched)
    typist, screen = Typist(), Screen(capsys)
    task = asyncio.create_task(ReplSession.interactive(command, read=typist))
    await screen.shows("wants to talk")  # held state needs no subscription
    bus.flush_gate.set()
    await eventually(lambda: bool(readiness) and readiness[-1] and len(readiness) > 1)
    assert readiness[0] is False
    typist.end_input()
    await asyncio.gather(task, return_exceptions=True)
    await holder.close()


async def test_an_invite_nobody_answers_times_out_and_is_withdrawn(
    talkers: Talkers, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("biff.cli_talk.INVITE_WAIT_SECONDS", 0.3)
    monkeypatch.setattr("biff.cli_console._WAKE_SECONDS", 0.1)
    talkers.repl.typist.type(f"talk {talkers.eric_at}")
    await eventually(lambda: "timed out: no one answered" in _err(talkers.repl))
    await eventually(lambda: _withdrawn(talkers.bus))
