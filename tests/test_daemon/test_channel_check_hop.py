"""The channel check across the daemon hop.

``docs/specs/channel-check.tex``: the doctor's request becomes one
``channel_check_request`` event per accepted request (at least once to the
client, never a second event for one token), and ``channel_ack`` relays one
acknowledgement to the doctor's reply subject.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, cast

import pytest

from biff.channel_check_wire import CheckAck, CheckRequest
from biff.client.connection import DaemonError
from tests.test_daemon.push_relay import PushRelay
from tests.test_daemon.talk_bus import TalkBus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from biff.client.connection import DaemonClient
    from biff.frames import Event
    from tests.test_daemon.conftest import Harness

_SOON = 2.0
_QUIET = 0.3
_REPLY = "_INBOX.doctor1"


class Rig:
    def __init__(self, harness: Harness, bus: TalkBus) -> None:
        self.harness = harness
        self.bus = bus

    def acks(self, reply: str = _REPLY) -> list[CheckAck]:
        return [
            CheckAck.from_payload(payload)
            for subject, payload in self.bus.published
            if subject == reply
        ]


@pytest.fixture
async def rig(harness: Harness, tmp_path: Path) -> AsyncIterator[Rig]:
    bus = TalkBus()

    def build(path: Path) -> PushRelay:
        return PushRelay(path, bus)

    harness.factory.relay_type = build
    yield Rig(harness, bus)


async def _next(client: DaemonClient, timeout: float = _SOON) -> Event:
    return await asyncio.wait_for(client.next_event(), timeout=timeout)


async def _session(rig: Rig, session_id: str = "s-kai") -> tuple[DaemonClient, str]:
    client = await rig.harness.connect(session_id, "kai", client="mcp")
    status = cast("dict[str, dict[str, str]]", await client.request("status"))
    key = status["data"]["session_key"]
    await client.subscribe()
    assert (await _next(client)).kind == "unread"
    await rig.bus.flush()
    return client, key


async def _ask(rig: Rig, key: str, reply: str = _REPLY) -> None:
    await rig.bus.publish(f"channel.check.{key}", CheckRequest(reply).to_payload())


def _token(event: Event) -> str:
    assert event.kind == "channel_check_request"
    return cast("dict[str, str]", event.data)["token"]


async def test_a_request_becomes_one_event_with_a_128_bit_token(rig: Rig) -> None:
    kai, key = await _session(rig)
    await _ask(rig, key)
    event = await _next(kai)
    assert re.fullmatch(r"[0-9a-f]{32}", _token(event))
    assert _REPLY not in event.model_dump_json()  # the event never names the subject


async def test_a_second_request_inside_the_window_makes_no_second_event(
    rig: Rig,
) -> None:
    kai, key = await _session(rig)
    await _ask(rig, key)
    await _next(kai)
    await _ask(rig, key, "_INBOX.doctor2")
    with pytest.raises(TimeoutError):
        await _next(kai, timeout=_QUIET)


async def test_a_request_naming_a_foreign_subject_is_refused(rig: Rig) -> None:
    kai, key = await _session(rig)
    await _ask(rig, key, "attacker.subject")
    with pytest.raises(TimeoutError):
        await _next(kai, timeout=_QUIET)


async def test_the_acknowledgement_is_relayed_once_to_the_reply_subject(
    rig: Rig,
) -> None:
    kai, key = await _session(rig)
    await _ask(rig, key)
    token = _token(await _next(kai))
    reply = cast("dict[str, object]", await kai.request("channel_ack", token=token))
    assert (reply["error"], reply["text"]) == (False, "biff channel live.")
    assert [(a.session_key, a.token) for a in rig.acks()] == [(key, token)]


async def test_a_repeated_acknowledgement_changes_nothing(rig: Rig) -> None:
    kai, key = await _session(rig)
    await _ask(rig, key)
    token = _token(await _next(kai))
    await kai.request("channel_ack", token=token)
    again = cast("dict[str, object]", await kai.request("channel_ack", token=token))
    assert again["text"] == "biff channel token not accepted; nothing changed."
    assert len(rig.acks()) == 1


async def test_a_wrong_token_relays_nothing(rig: Rig) -> None:
    kai, key = await _session(rig)
    await _ask(rig, key)
    await _next(kai)
    wrong = cast("dict[str, object]", await kai.request("channel_ack", token="0" * 32))
    assert wrong["text"] == "biff channel token not accepted; nothing changed."
    assert rig.acks() == []


async def test_a_new_subscription_gets_the_pending_request_again(rig: Rig) -> None:
    kai, key = await _session(rig)
    await _ask(rig, key)
    token = _token(await _next(kai))
    again = await rig.harness.connect("s-kai", "kai", client="mcp")
    await again.subscribe()
    kinds = [(await _next(again)).kind, (await _next(again)).kind]
    assert kinds == ["unread", "channel_check_request"]  # delivered once more
    await kai.request("channel_ack", token=token)
    assert len(rig.acks()) == 1


async def test_a_request_replayed_after_the_acknowledgement_makes_nothing(
    rig: Rig,
) -> None:
    kai, key = await _session(rig)
    await _ask(rig, key)
    token = _token(await _next(kai))
    await kai.request("channel_ack", token=token)
    again = await rig.harness.connect("s-kai", "kai", client="mcp")
    await again.subscribe()
    assert (await _next(again)).kind == "unread"
    with pytest.raises(TimeoutError):
        await _next(again, timeout=_QUIET)  # no second event for a spent token
    await again.request("channel_ack", token=token)
    assert len(rig.acks()) == 1  # and no second relayed acknowledgement


async def test_the_doctor_is_told_when_the_session_acknowledges(rig: Rig) -> None:
    kai, key = await _session(rig)
    doctor = await rig.harness.connect("s-doctor", "kai", client="cli")
    check = asyncio.ensure_future(doctor.request("channel_check", to=key, seconds=5))
    token = _token(await _next(kai))
    await kai.request("channel_ack", token=token)
    reply = cast("dict[str, dict[str, bool]]", await check)
    assert reply["data"]["confirmed"] is True


async def test_the_doctor_reports_a_session_that_never_acknowledges(rig: Rig) -> None:
    _, key = await _session(rig)
    doctor = await rig.harness.connect("s-doctor", "kai", client="cli")
    reply = cast(
        "dict[str, dict[str, bool]]",
        await doctor.request("channel_check", to=key, seconds=0),
    )
    assert reply["data"]["confirmed"] is False


async def test_a_doctor_on_a_local_relay_is_told_it_needs_nats(
    harness: Harness,
) -> None:
    doctor = await harness.connect("s-doctor", "kai", client="cli")
    reply = cast(
        "dict[str, object]", await doctor.request("channel_check", to="kai:abc")
    )
    assert reply["error"] is True


async def test_channel_ack_refuses_params_it_does_not_know(rig: Rig) -> None:
    kai, _ = await _session(rig)
    with pytest.raises(DaemonError):
        await kai.request("channel_ack", token="x", session_key="eric:abc")


async def test_a_pending_check_never_precedes_the_summary(rig: Rig) -> None:
    kai, key = await _session(rig)
    await _ask(rig, key)
    await _next(kai)
    again = await rig.harness.connect("s-kai", "kai", client="mcp")
    await again.subscribe()
    kinds = [(await _next(again)).kind, (await _next(again)).kind]
    assert kinds[0] == "unread"
    assert kinds[1] == "channel_check_request"
