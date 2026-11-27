"""Two sessions hold a conversation through biffd: invite, accept, lines, end."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, cast

import pytest

from biff._formatting import HEADER_PREFIX
from biff.daemon.talk_methods import TalkMethods
from biff.formatting import format_talk_end
from biff.invite_expiry import InviteExpiry
from biff.talk_state import MAX_TALK_QUEUE
from tests.test_daemon.talk_bus import TalkBus

if TYPE_CHECKING:
    from collections.abc import Callable

    from biff.client.connection import DaemonClient
    from biff.frames import Event
    from tests.test_daemon.conftest import Harness


def _data(result: object) -> dict[str, object]:
    return cast("dict[str, dict[str, object]]", result)["data"]


async def _tty(client: DaemonClient) -> str:
    return cast("str", _data(await client.request("status"))["tty_name"])


class _Pair:
    """Kai and eric attached to one daemon, each knowing the other's address."""

    def __init__(self, kai: DaemonClient, eric: DaemonClient) -> None:
        self.kai = kai
        self.eric = eric
        self.kai_at = ""
        self.eric_at = ""

    async def introduce(self) -> None:
        self.kai_at = f"kai:{await _tty(self.kai)}"
        self.eric_at = f"eric:{await _tty(self.eric)}"

    async def say(self, who: DaemonClient, to: str, text: str) -> None:
        reply = cast(
            "dict[str, object]", await who.request("talk", to=to, message=text)
        )
        assert reply["error"] is False, reply["text"]

    async def read(self, who: DaemonClient) -> dict[str, object]:
        return _data(await who.request("talk_read"))

    async def connect(self) -> None:
        """Kai invites, eric accepts, kai learns of the accept."""
        await self.say(self.kai, self.eric_at, "")
        await self.read(self.eric)
        await self.say(self.eric, self.kai_at, "")
        await self.read(self.kai)


@pytest.fixture
async def pair(harness: Harness, bus: TalkBus) -> _Pair:
    assert bus is not None
    both = _Pair(
        await harness.connect("s-kai", "kai"), await harness.connect("s-eric", "eric")
    )
    await both.introduce()
    return both


async def test_an_invite_is_pending_for_the_invitee(pair: _Pair) -> None:
    await pair.say(pair.kai, pair.eric_at, "")
    seen = await pair.read(pair.eric)
    invites = cast("list[dict[str, str]]", seen["invites"])
    assert [(i["from"], i["tty"]) for i in invites] == [
        ("kai", pair.kai_at.split(":")[1])
    ]


async def test_three_lines_each_way_arrive_exactly(pair: _Pair) -> None:
    await pair.connect()
    for n in (1, 2, 3):
        await pair.say(pair.kai, pair.eric_at, f"kai says {n}")
    for n in (1, 2, 3):
        await pair.say(pair.eric, pair.kai_at, f"eric says {n}")
    at_eric = await pair.read(pair.eric)
    at_kai = await pair.read(pair.kai)
    assert at_eric["lines"] == [
        f"{HEADER_PREFIX}{pair.kai_at}: kai says {n}" for n in (1, 2, 3)
    ]
    assert at_kai["lines"] == [
        f"{HEADER_PREFIX}{pair.eric_at}: eric says {n}" for n in (1, 2, 3)
    ]


async def test_a_line_is_drained_once(pair: _Pair) -> None:
    await pair.connect()
    await pair.say(pair.kai, pair.eric_at, "once")
    await pair.read(pair.eric)
    assert (await pair.read(pair.eric))["lines"] == []


@pytest.mark.parametrize("hangs_up", ["kai", "eric"])
async def test_either_side_can_end_the_talk(pair: _Pair, hangs_up: str) -> None:
    await pair.connect()
    leaver, stayer = (
        (pair.kai, pair.eric) if hangs_up == "kai" else (pair.eric, pair.kai)
    )
    label = pair.kai_at if hangs_up == "kai" else pair.eric_at
    ended = cast("dict[str, object]", await leaver.request("talk_end"))
    assert _data(ended) == {"ended": True}
    seen = await pair.read(stayer)
    assert (seen["ended"], seen["lines"]) == (True, [format_talk_end(label)])


async def test_ending_with_no_talk_says_so(pair: _Pair) -> None:
    ended = cast("dict[str, object]", await pair.kai.request("talk_end"))
    assert _data(ended) == {"ended": False}


async def test_an_unanswered_invite_expires(
    pair: _Pair, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pair.say(pair.kai, pair.eric_at, "")
    monkeypatch.setattr("biff.talk_state._EXPIRY", InviteExpiry(0.0))
    seen = await pair.read(pair.eric)
    assert seen["invites"] == []


async def test_the_queue_keeps_the_newest_hundred_lines(pair: _Pair) -> None:
    await pair.connect()
    total = MAX_TALK_QUEUE + 20
    for n in range(total):
        await pair.say(pair.kai, pair.eric_at, f"line {n}")
    seen = await pair.read(pair.eric)
    lines = cast("list[str]", seen["lines"])
    assert len(lines) == MAX_TALK_QUEUE
    assert seen["dropped"] == 20  # the overflow is counted, not silent
    assert (await pair.read(pair.eric))["dropped"] == 0  # and reported once
    assert lines[0].endswith(f"line {total - MAX_TALK_QUEUE}")
    assert lines[-1].endswith(f"line {total - 1}")


async def test_talk_needs_a_relay_that_carries_it(harness: Harness) -> None:
    client = await harness.connect("s-kai", "kai")
    reply = cast(
        "dict[str, object]", await client.request("talk", to="eric:tty1", message="")
    )
    assert reply["error"] is True
    assert reply["text"] == "Talk requires a NATS relay connection."


async def _notice(client: DaemonClient) -> Event:
    """Return the next talk event, skipping the mail and wall summaries."""
    while True:
        event = await asyncio.wait_for(client.next_event(), timeout=5.0)
        if event.kind.startswith("talk_"):
            return event


async def test_an_invite_is_announced_with_who_and_not_what(pair: _Pair) -> None:
    await pair.eric.subscribe()
    await pair.say(pair.kai, pair.eric_at, "secret opening")
    event = await _notice(pair.eric)
    assert (event.kind, event.data) == (
        "talk_invite",
        {"from": "kai", "tty": pair.kai_at.split(":")[1], "pending": 1},
    )
    assert "secret opening" not in event.model_dump_json()


async def test_an_accept_wakes_the_inviter(pair: _Pair) -> None:
    await pair.say(pair.kai, pair.eric_at, "")
    await pair.read(pair.eric)
    await pair.kai.subscribe()
    await pair.say(pair.eric, pair.kai_at, "")
    assert (await _notice(pair.kai)).kind == "talk_accept"


async def test_a_line_is_announced_with_the_count_waiting(pair: _Pair) -> None:
    await pair.connect()
    await pair.eric.subscribe()
    await pair.say(pair.kai, pair.eric_at, "one")
    await pair.say(pair.kai, pair.eric_at, "two")
    events = [await _notice(pair.eric), await _notice(pair.eric)]
    counts = [cast("dict[str, int]", e.data)["pending"] for e in events]
    assert [(e.kind, n) for e, n in zip(events, counts, strict=True)] == [
        ("talk_line", 1),
        ("talk_line", 2),
    ]


async def test_a_hangup_is_announced(pair: _Pair) -> None:
    await pair.connect()
    await pair.eric.subscribe()
    await pair.kai.request("talk_end")
    assert (await _notice(pair.eric)).kind == "talk_end"


async def _eventually(check: Callable[[], bool]) -> None:
    async with asyncio.timeout(5.0):
        while not check():
            await asyncio.sleep(0.01)


async def _subject(client: DaemonClient) -> str:
    key = cast("str", _data(await client.request("status"))["session_key"])
    return f"talk.notify.{key}"


async def test_the_subscription_lasts_as_long_as_a_client_is_attached(
    pair: _Pair, bus: TalkBus
) -> None:
    subject = await _subject(pair.eric)
    await _eventually(lambda: bus.subscriber_count(subject) == 1)
    await pair.eric.close()
    await _eventually(lambda: bus.subscriber_count(subject) == 0)


async def test_a_new_relay_client_gets_a_new_subscription(
    pair: _Pair, bus: TalkBus
) -> None:
    subject = await _subject(pair.eric)
    await _eventually(lambda: bus.subscriber_count(subject) == 1)
    before = bus.subscribes
    bus.relays[0].generation += 1  # the relay dialled a fresh client
    await _eventually(lambda: bus.subscribes > before)
    await pair.say(pair.kai, pair.eric_at, "")
    assert (await pair.read(pair.eric))["invites"] != []
    assert bus.subscriber_count(subject) == 1


async def test_a_session_holds_its_conversation_while_a_client_reconnects(
    pair: _Pair, harness: Harness
) -> None:
    await pair.connect()
    await pair.say(pair.kai, pair.eric_at, "while you were away")
    again = await harness.connect("s-eric", "eric")
    seen = _data(await again.request("talk_read"))
    assert seen["lines"] == [f"{HEADER_PREFIX}{pair.kai_at}: while you were away"]


async def test_every_listening_connection_of_a_session_is_told(
    pair: _Pair, harness: Harness
) -> None:
    second = await harness.connect("s-eric", "eric")
    await pair.eric.subscribe()
    await second.subscribe()
    await pair.say(pair.kai, pair.eric_at, "")
    assert (await _notice(pair.eric)).kind == "talk_invite"
    assert (await _notice(second)).kind == "talk_invite"


async def test_a_failed_resubscribe_is_retried_and_the_feed_survives(
    pair: _Pair, bus: TalkBus
) -> None:
    subject = await _subject(pair.eric)
    await _eventually(lambda: bus.subscriber_count(subject) == 1)
    relay = bus.relays[0]
    relay.down = True
    relay.generation += 1  # the client was replaced and the next dial fails
    await asyncio.sleep(0.2)  # several ticks fail
    relay.down = False
    # Frames are ephemeral: one sent before the feed subscribes again is lost.
    await _eventually(lambda: bus.subscriber_count(subject) == 1)
    await pair.say(pair.kai, pair.eric_at, "")
    assert (await pair.read(pair.eric))["invites"] != []


async def test_a_session_attached_while_the_relay_is_down_subscribes_when_it_returns(
    harness: Harness, bus: TalkBus
) -> None:
    kai = await harness.connect("s-kai", "kai")
    await _tty(kai)  # the relay is built and answering
    bus.relays[0].down = True
    third = await harness.connect("s-third", "third")
    await asyncio.sleep(0.2)
    bus.relays[0].down = False
    third_subject = await _subject(third)
    await _eventually(lambda: bus.subscriber_count(third_subject) == 1)
    third_at = third_subject.removeprefix("talk.notify.")
    await kai.request("talk", to=third_at, message="")
    seen = _data(await third.request("talk_read"))
    invites = cast("list[dict[str, str]]", seen["invites"])
    assert [i["from"] for i in invites] == ["kai"]


async def test_a_failing_resubscribe_warns_once_then_recovers(
    pair: _Pair, bus: TalkBus, caplog: pytest.LogCaptureFixture
) -> None:
    subject = await _subject(pair.eric)
    await _eventually(lambda: bus.subscriber_count(subject) == 1)
    relay = bus.relays[0]
    with caplog.at_level(logging.INFO, logger="biff.daemon.talk_feed"):
        relay.down = True
        relay.generation += 1
        await asyncio.sleep(0.3)  # many ticks fail
        relay.down = False
        before = bus.subscribes
        await _eventually(lambda: bus.subscribes > before)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2  # once per session, however many ticks failed
    assert "recovered" in caplog.records[-1].getMessage()


async def test_when_the_last_client_leaves_mid_talk_the_peer_is_hung_up_on(
    pair: _Pair,
) -> None:
    await pair.connect()
    await pair.kai.close()  # no hang-up: the client died
    ended: dict[str, object] = {}

    async def peer_told() -> None:
        while not ended.get("ended"):
            ended.update(await pair.read(pair.eric))
            await asyncio.sleep(0.01)

    async with asyncio.timeout(5.0):
        await peer_told()
    assert ended["lines"] == [format_talk_end(pair.kai_at)]


async def test_a_session_whose_last_client_left_mid_talk_is_idle_again(
    pair: _Pair, harness: Harness
) -> None:
    await pair.connect()
    await pair.kai.close()
    again = await harness.connect("s-kai", "kai")
    async with asyncio.timeout(5.0):
        while True:
            standing = cast("dict[str, object]", await again.request("talk_read"))
            if _data(standing)["phase"] == "idle":
                break
            await asyncio.sleep(0.01)
    third = cast("dict[str, object]", await again.request("talk_end"))
    assert _data(third) == {"ended": False}


async def test_a_client_that_reattaches_at_once_keeps_its_talk(
    pair: _Pair, harness: Harness
) -> None:
    await pair.connect()
    held = await harness.connect("s-kai", "kai")  # a second client of the session
    await pair.kai.close()  # not the last
    await pair.say(held, pair.eric_at, "still here")
    assert (await pair.read(pair.eric))["lines"] == [
        f"{HEADER_PREFIX}{pair.kai_at}: still here"
    ]


async def test_ending_a_talk_resets_it_even_when_the_session_row_is_unreachable(
    pair: _Pair, bus: TalkBus
) -> None:
    await pair.connect()
    bus.relays[0].stale = True  # presence reads and writes now time out
    ended = cast("dict[str, object]", await pair.kai.request("talk_end"))
    bus.relays[0].stale = False
    assert _data(ended) == {"ended": True}
    again = cast("dict[str, object]", await pair.kai.request("talk_end"))
    assert _data(again) == {"ended": False}


async def test_talking_to_someone_whose_invite_is_unread_accepts_it(
    pair: _Pair,
) -> None:
    await pair.say(pair.kai, pair.eric_at, "")  # eric has not read it
    accepted = cast(
        "dict[str, object]",
        await pair.eric.request("talk", to=pair.kai_at, message=""),
    )
    assert _data(accepted) == {"phase": "connected", "peer": pair.kai_at}


async def test_the_opening_line_of_an_invite_reaches_the_peer_after_the_accept(
    pair: _Pair,
) -> None:
    await pair.say(pair.kai, pair.eric_at, "opening line")
    await pair.read(pair.eric)
    await pair.say(pair.eric, pair.kai_at, "")
    at_kai = await pair.read(pair.kai)  # learns of the accept, sends the opening
    assert at_kai["opening"] == "opening line"
    assert (await pair.read(pair.eric))["lines"] == [
        f"{HEADER_PREFIX}{pair.kai_at}: opening line"
    ]


async def test_an_invite_without_an_opening_line_sends_none(pair: _Pair) -> None:
    await pair.connect()
    assert (await pair.read(pair.eric))["lines"] == []


async def test_a_publish_failure_while_ending_resets_the_talk_and_says_so(
    pair: _Pair, bus: TalkBus, caplog: pytest.LogCaptureFixture
) -> None:
    await pair.connect()
    bus.broken = True
    with caplog.at_level(logging.INFO, logger="biff.commands.talk"):
        ended = cast("dict[str, object]", await pair.kai.request("talk_end"))
    bus.broken = False
    assert (ended["error"], _data(ended)) == (True, {"ended": True})
    again = cast("dict[str, object]", await pair.kai.request("talk_end"))
    assert _data(again) == {"ended": False}  # kai is idle however eric fared
    assert all(r.levelno == logging.INFO for r in caplog.records)
    assert "wedged" not in caplog.text


async def test_an_invite_that_cannot_be_published_leaves_the_session_idle(
    pair: _Pair, bus: TalkBus
) -> None:
    bus.broken = True
    reply = cast(
        "dict[str, object]",
        await pair.kai.request("talk", to=pair.eric_at, message=""),
    )
    bus.broken = False
    assert (reply["error"], _data(reply)["phase"]) == (True, "idle")


async def test_an_accept_that_cannot_be_published_keeps_the_invite_acceptable(
    pair: _Pair, bus: TalkBus
) -> None:
    await pair.say(pair.kai, pair.eric_at, "")
    await pair.read(pair.eric)
    bus.broken = True
    failed = cast(
        "dict[str, object]",
        await pair.eric.request("talk", to=pair.kai_at, message=""),
    )
    bus.broken = False
    assert (failed["error"], _data(failed)["phase"]) == (True, "idle")
    retried = cast(
        "dict[str, object]",
        await pair.eric.request("talk", to=pair.kai_at, message=""),
    )
    assert (retried["error"], _data(retried)["phase"]) == (False, "connected")


async def test_talk_reports_where_the_conversation_stands(pair: _Pair) -> None:
    invited = cast(
        "dict[str, object]",
        await pair.kai.request("talk", to=pair.eric_at, message=""),
    )
    assert _data(invited) == {"phase": "inviting", "peer": pair.eric_at}


_HOSTILE_TTY = "tty1\x1b[2J\x1b]52;c;cHduZWQ=\x07\nPWN"


async def test_a_hostile_invite_reaches_the_reader_without_controls(
    pair: _Pair, bus: TalkBus
) -> None:
    subject = await _subject(pair.eric)
    frame = {
        "type": "invite",
        "from": "kai",
        "from_tty": _HOSTILE_TTY,
        "from_key": "kai:abc123",
        "to_key": subject.removeprefix("talk.notify."),
        "body": "",
    }
    await bus.publish(subject, json.dumps(frame).encode())
    seen = _data(await pair.eric.request("talk_read"))
    assert len(cast("list[object]", seen["invites"])) == 1  # the frame arrived
    shown = json.dumps(seen["invites"]) + "".join(cast("list[str]", seen["lines"]))
    assert "\\u001b" not in shown
    assert "\\n" not in shown
    assert "\\u0007" not in shown


async def test_a_notice_names_a_hostile_sender_without_controls(
    pair: _Pair, bus: TalkBus
) -> None:
    await pair.eric.subscribe()
    subject = await _subject(pair.eric)
    frame = {
        "type": "message",
        "from": "ka\x1b[2Ji",
        "from_tty": _HOSTILE_TTY,
        "from_key": "kai:abc123",
        "to_key": subject.removeprefix("talk.notify."),
        "body": "x",
    }
    await bus.publish(subject, json.dumps(frame).encode())
    shown = (await _notice(pair.eric)).model_dump_json()
    assert "\\u001b" not in shown
    assert "\\u0007" not in shown
    assert "\\n" not in shown


async def test_a_bug_in_the_feed_does_not_end_talk_for_the_session(
    pair: _Pair, bus: TalkBus, caplog: pytest.LogCaptureFixture
) -> None:
    subject = await _subject(pair.eric)
    await _eventually(lambda: bus.subscriber_count(subject) == 1)
    relay = bus.relays[0]
    relay.crash = RuntimeError("a bug, not a relay outage")
    relay.generation += 1
    await asyncio.sleep(0.2)  # several ticks hit the bug
    relay.crash = None
    await _eventually(lambda: bus.subscriber_count(subject) == 1)
    await pair.say(pair.kai, pair.eric_at, "")
    assert (await pair.read(pair.eric))["invites"] != []
    assert any(r.exc_info for r in caplog.records)


async def test_slow_talk_setup_never_delays_commands_that_do_not_talk(
    harness: Harness, bus: TalkBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus.subscribe_block = asyncio.Event()  # the subscription never completes
    monkeypatch.setattr(TalkMethods, "READY_SECONDS", 0.2)
    started = asyncio.get_running_loop().time()
    kai = await harness.connect("s-kai", "kai")
    assert asyncio.get_running_loop().time() - started < 0.3  # attach is prompt
    for method in ("who", "status", "read"):
        await asyncio.wait_for(kai.request(method), timeout=1.0)
    started = asyncio.get_running_loop().time()
    reply = cast(
        "dict[str, object]", await kai.request("talk", to="eric:tty1", message="")
    )
    assert asyncio.get_running_loop().time() - started < 1.0
    assert reply["error"] is True
    assert "not ready" in cast("str", reply["text"])


async def test_talk_is_ready_only_once_the_server_has_the_subscription(
    harness: Harness, bus: TalkBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(TalkMethods, "READY_SECONDS", 0.2)
    bus.flush_gate = asyncio.Event()  # the subscription is queued, not confirmed
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    eric_at = f"eric:{await _tty(eric)}"
    early = cast("dict[str, object]", await eric.request("talk_read"))
    assert (early["error"], "not ready" in cast("str", early["text"])) == (True, True)
    bus.flush_gate.set()  # the server confirms
    async with asyncio.timeout(5.0):
        while True:
            sent = cast(
                "dict[str, object]",
                await kai.request("talk", to=eric_at, message=""),
            )
            if sent["error"] is False:
                break
            await asyncio.sleep(0.01)
    async with asyncio.timeout(5.0):
        while not _data(await eric.request("talk_read"))["invites"]:
            await asyncio.sleep(0.01)


async def test_an_explicit_tty_is_not_redirected_to_another_ttys_invite(
    harness: Harness, bus: TalkBus
) -> None:
    assert bus is not None
    kai = await harness.connect("s-kai", "kai")
    bob1 = await harness.connect("s-bob1", "bob")
    bob2 = await harness.connect("s-bob2", "bob")
    kai_at, bob1_at, bob2_at = (
        f"kai:{await _tty(kai)}",
        f"bob:{await _tty(bob1)}",
        f"bob:{await _tty(bob2)}",
    )
    await bob1.request("talk", to=kai_at, message="")  # bob's first tty invites kai
    await asyncio.sleep(0.1)  # the invite is queued at kai, unread
    sent = cast(
        "dict[str, object]", await kai.request("talk", to=bob2_at, message="hi")
    )
    assert _data(sent) == {"phase": "inviting", "peer": bob2_at}
    seen = _data(await kai.request("talk_read"))
    assert [i["accept"] for i in cast("list[dict[str, str]]", seen["invites"])] == [
        f"talk {bob1_at}"
    ]


async def test_a_last_detach_with_the_accept_still_queued_ends_the_peer(
    pair: _Pair,
) -> None:
    await pair.say(pair.kai, pair.eric_at, "")
    await pair.read(pair.eric)
    await pair.say(pair.eric, pair.kai_at, "")  # eric is connected; kai has not read
    await pair.kai.close()
    seen: dict[str, object] = {}
    async with asyncio.timeout(5.0):
        while not seen.get("ended"):
            seen = await pair.read(pair.eric)
            await asyncio.sleep(0.01)
    assert seen["phase"] == "idle"
