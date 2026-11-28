"""Channel events reach a Claude Code session through biffd (DES-087, daemon path).

``docs/specs/channel-check.tex`` and ``notification-mail-event.tex``: an event is a
summary, never a body; a mail event names the count unread now and is ordered by the
inbox mark; a channel check token is made by biffd and acknowledged through it.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, cast

import pytest

from biff.channel_check_wire import CheckAck, CheckRequest
from biff.client.connection import ClientIdentity, DaemonClient
from biff.server.channel_check import ChannelCheck
from biff.server.channel_rate import RateLimit
from biff.server.daemon_channel import DaemonChannelFeed
from biff.server.mcp_link import McpLink
from biff.session_routing import SessionRouting
from tests.test_daemon.push_relay import PushRelay
from tests.test_daemon.talk_bus import TalkBus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping
    from pathlib import Path

    from tests.test_daemon.conftest import Harness

_SOON = 3.0
_QUIET = 0.4
_REPLY = "_INBOX.doctor1"


class _Sink:
    """The channel connection: records each event the emitter hands it."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, str]]] = []

    @property
    def is_open(self) -> bool:
        return True

    async def send(self, content: str, meta: Mapping[str, str]) -> None:
        self.sent.append((content, dict(meta)))

    def kinds(self) -> list[str]:
        return [meta["kind"] for _, meta in self.sent]


class Rig:
    def __init__(self, harness: Harness, bus: TalkBus, limit: RateLimit | None) -> None:
        self.harness = harness
        self.bus = bus
        self.sink = _Sink()
        self.link = McpLink(user="kai", cwd="/work/repo", connect=self._connect)
        self.feed = DaemonChannelFeed(
            link=self.link, check=ChannelCheck(), retry_seconds=0.05, limit=limit
        )
        self.opens = 0
        self.refuse_first = 0

    async def _connect(self, identity: ClientIdentity) -> DaemonClient:
        self.opens += 1
        if self.refuse_first >= self.opens:
            msg = "biffd is down"
            raise ConnectionError(msg)
        return await DaemonClient.connect(
            run_dir=self.harness.run_dir,
            port=self.harness.daemon.port,
            identity=identity,
        )

    async def start(self, session_id: str = "s-kai") -> None:
        self.feed.emitter.serve(self.sink)
        self.feed.emitter.initialized()
        self.feed.bind(SessionRouting(session_id))

    async def wait_for(self, check: Callable[[], bool]) -> None:
        deadline = asyncio.get_running_loop().time() + _SOON
        while not check():
            assert asyncio.get_running_loop().time() < deadline, self.sink.kinds()
            await asyncio.sleep(0.02)


@pytest.fixture
async def rig(harness: Harness, bus_relay: TalkBus) -> AsyncIterator[Rig]:
    """A rig whose rate limit is short enough for a test to see two events."""
    made = Rig(harness, bus_relay, RateLimit(per_kind=0.1))
    yield made
    await made.feed.close()


@pytest.fixture
async def limited(harness: Harness, bus_relay: TalkBus) -> AsyncIterator[Rig]:
    """A rig with the design's rate limit."""
    made = Rig(harness, bus_relay, None)
    yield made
    await made.feed.close()


@pytest.fixture
def bus_relay(harness: Harness) -> TalkBus:
    shared = TalkBus()

    def build(path: Path) -> PushRelay:
        return PushRelay(path, shared)

    harness.factory.relay_type = build
    return shared


async def _eric(rig: Rig) -> DaemonClient:
    return await rig.harness.connect("s-eric", "eric", client="mcp")


async def _tty(client: DaemonClient) -> str:
    status = cast("dict[str, dict[str, str]]", await client.request("status"))
    return status["data"]["tty_name"]


async def test_mail_reaches_the_session_as_one_summary_event(rig: Rig) -> None:
    await rig.start()
    eric = await _eric(rig)

    await eric.request("write", to="kai", message="the body must not travel")

    await rig.wait_for(lambda: "mail" in rig.sink.kinds())
    content, meta = rig.sink.sent[rig.sink.kinds().index("mail")]
    assert meta == {"kind": "mail", "count": "1"}
    assert "the body must not travel" not in content


async def test_a_second_mail_inside_the_rate_window_is_held(limited: Rig) -> None:
    await limited.start()
    eric = await _eric(limited)
    await eric.request("write", to="kai", message="one")
    await limited.wait_for(lambda: "mail" in limited.sink.kinds())

    await eric.request("write", to="kai", message="two")
    await asyncio.sleep(_QUIET)

    assert limited.sink.kinds() == ["mail"]  # held for the kind's 30 s window
    assert "mail" in limited.feed.emitter.held


async def test_mail_waiting_before_the_session_started_is_announced(rig: Rig) -> None:
    eric = await _eric(rig)
    await rig.harness.connect("s-kai", "kai", client="mcp")  # kai exists
    await eric.request("write", to="kai", message="while you were away")

    await rig.start()

    await rig.wait_for(lambda: "mail" in rig.sink.kinds())


async def test_a_wall_reaches_the_session_but_not_its_poster(rig: Rig) -> None:
    await rig.start()
    eric = await _eric(rig)

    await eric.request("wall", message="deploy freeze", duration="1h")

    await rig.wait_for(lambda: "wall" in rig.sink.kinds())
    assert rig.sink.kinds().count("wall") == 1


async def test_a_talk_invite_and_the_partners_line_reach_the_session(
    rig: Rig,
) -> None:
    await rig.start()
    tools = await rig.harness.connect("s-kai", "kai", client="mcp")  # kai's tools
    eric = await _eric(rig)
    kai_at = f"kai:{await _tty(tools)}"
    eric_at = f"eric:{await _tty(eric)}"

    await eric.request("talk", to=kai_at, message="")
    await rig.wait_for(lambda: "talk_invite" in rig.sink.kinds())

    await tools.request("talk_read")  # kai's tools read the invite
    await tools.request("talk", to=eric_at, message="")  # and accept it
    await eric.request("talk_read")  # eric learns of the accept
    await eric.request("talk", to=kai_at, message="hello")
    await rig.wait_for(lambda: "talk_message" in rig.sink.kinds())


async def test_mesg_off_holds_events_and_mesg_on_sends_them(rig: Rig) -> None:
    await rig.start()
    tools = await rig.harness.connect("s-kai", "kai", client="mcp")
    await tools.request("mesg", value="n")
    eric = await _eric(rig)

    await eric.request("write", to="kai", message="held")
    await asyncio.sleep(_QUIET)
    assert rig.sink.kinds() == []

    await tools.request("mesg", value="y")
    rig.feed.emitter.tool_called()  # what the tool call does in the server

    await rig.wait_for(lambda: "mail" in rig.sink.kinds())


async def test_a_second_client_attaches_without_disturbing_the_feed(
    rig: Rig,
) -> None:
    await rig.start()
    eric = await _eric(rig)
    await eric.request("write", to="kai", message="one")
    await rig.wait_for(lambda: "mail" in rig.sink.kinds())

    second = await rig.harness.connect("s-kai", "kai", client="mcp")
    await second.request("status")
    await eric.request("wall", message="after the second client", duration="1h")

    await rig.wait_for(lambda: "wall" in rig.sink.kinds())  # the feed still hears
    await second.close()
    await eric.request("write", to="kai", message="two")
    await rig.wait_for(lambda: rig.sink.sent[-1][1].get("count") == "2")


async def test_a_feed_that_cannot_reach_biffd_opens_again_and_announces_mail(
    rig: Rig,
) -> None:
    eric = await _eric(rig)
    await rig.harness.connect("s-kai", "kai", client="mcp")
    await eric.request("write", to="kai", message="waiting")
    rig.refuse_first = 2  # biffd is down for the first two opens

    await rig.start()

    await rig.wait_for(lambda: "mail" in rig.sink.kinds())
    assert rig.opens >= 3


async def test_a_channel_check_event_carries_biffds_token_and_the_ack_goes_through(
    rig: Rig,
) -> None:
    await rig.start()
    tools = await rig.harness.connect("s-kai", "kai", client="mcp")
    key = cast("dict[str, dict[str, str]]", await tools.request("status"))["data"][
        "session_key"
    ]
    await rig.wait_for(lambda: rig.opens >= 1)
    await asyncio.sleep(0.2)  # the subscription is up

    await rig.bus.publish(f"channel.check.{key}", CheckRequest(_REPLY).to_payload())

    await rig.wait_for(lambda: "channel_check" in rig.sink.kinds())
    content = rig.sink.sent[rig.sink.kinds().index("channel_check")][0]
    token = cast("re.Match[str]", re.search(r"token ([0-9a-f]{32})", content))[1]
    reply = cast("dict[str, str]", await tools.request("channel_ack", token=token))
    assert reply["text"] == "biff channel live."
    acks = [
        CheckAck.from_payload(payload)
        for subject, payload in rig.bus.published
        if subject == _REPLY
    ]
    assert [a.token for a in acks] == [token]
