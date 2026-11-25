"""The channel check over a real NATS server: request, one event, ack (DES-087)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from fastmcp.tools.function_tool import FunctionTool

from biff.channel_check_wire import CheckAck, CheckRequest
from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.channel_check_sub import ChannelCheckSub
from tests._state import create_state

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from biff.nats_relay import NatsRelay
    from biff.server.state import ServerState

pytestmark = pytest.mark.nats_local

_KEY = "kai:tty1"


class _Link:
    """The client's end of the channel: records every event."""

    is_open = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, str]]] = []

    async def send(self, content: str, meta: Mapping[str, str]) -> None:
        self.sent.append((content, dict(meta)))


class TestWire:
    async def test_a_request_reaches_the_sessions_check_subject(
        self, relay: NatsRelay
    ) -> None:
        nc = await relay.get_nc()
        seen: asyncio.Queue[bytes] = asyncio.Queue()

        async def on_msg(msg: object) -> None:
            await seen.put(getattr(msg, "data", b""))

        await nc.subscribe(relay.channel_check_subject(_KEY), cb=on_msg)  # pyright: ignore[reportUnknownMemberType]
        pending = await relay.request_channel_check(_KEY)
        try:
            request = CheckRequest.from_payload(await asyncio.wait_for(seen.get(), 2))
        finally:
            await pending.close()
        assert request.reply.startswith("_INBOX.")

    async def test_the_sessions_ack_confirms_the_wait(self, relay: NatsRelay) -> None:
        nc = await relay.get_nc()
        seen: asyncio.Queue[bytes] = asyncio.Queue()

        async def on_msg(msg: object) -> None:
            await seen.put(getattr(msg, "data", b""))

        await nc.subscribe(relay.channel_check_subject(_KEY), cb=on_msg)  # pyright: ignore[reportUnknownMemberType]
        pending = await relay.request_channel_check(_KEY)
        reply = CheckRequest.from_payload(await asyncio.wait_for(seen.get(), 2)).reply
        await relay.publish_channel_check_ack(reply, _KEY, "t" * 32)
        try:
            assert await pending.confirmed(2)
        finally:
            await pending.close()

    async def test_another_sessions_ack_does_not_confirm(
        self, relay: NatsRelay
    ) -> None:
        nc = await relay.get_nc()
        seen: asyncio.Queue[bytes] = asyncio.Queue()

        async def on_msg(msg: object) -> None:
            await seen.put(getattr(msg, "data", b""))

        await nc.subscribe(relay.channel_check_subject(_KEY), cb=on_msg)  # pyright: ignore[reportUnknownMemberType]
        pending = await relay.request_channel_check(_KEY)
        reply = CheckRequest.from_payload(await asyncio.wait_for(seen.get(), 2)).reply
        await relay.publish_channel_check_ack(reply, "kai:other", "t" * 32)
        try:
            assert not await pending.confirmed(0.3)
        finally:
            await pending.close()

    async def test_no_listening_session_means_not_confirmed(
        self, relay: NatsRelay
    ) -> None:
        pending = await relay.request_channel_check("kai:nobody")
        try:
            assert not await pending.confirmed(0.3)
        finally:
            await pending.close()

    def test_the_ack_round_trips_its_wire_form(self) -> None:
        ack = CheckAck("kai:tty1", "ab" * 16)
        assert CheckAck.from_payload(ack.to_payload()) == ack

    @pytest.mark.parametrize("data", [b"", b"1", b"[]", b'{"token": "x"}'])
    def test_a_malformed_ack_is_refused(self, data: bytes) -> None:
        with pytest.raises(ValueError, match="channel check"):
            CheckAck.from_payload(data)


class TestRoundTrip:
    """A relayed request makes exactly one check event; the ack replies."""

    @pytest.fixture
    def state(self, tmp_path: Path, relay: NatsRelay) -> ServerState:
        config = BiffConfig(user="kai", repo_name="_test-nats-unit")
        state = create_state(
            config, tmp_path, tty="tty1", hostname="h", pwd="/p", repo_root=tmp_path
        )
        object.__setattr__(state, "relay", relay)
        return state

    async def test_one_request_one_event_one_reply(
        self, state: ServerState, relay: NatsRelay
    ) -> None:
        link = _Link()
        state.emitter.serve(link)
        state.emitter.initialized()
        sub = ChannelCheckSub(state)
        await sub.start()
        try:
            pending = await relay.request_channel_check(state.session_key)
            for _ in range(100):  # the callback wakes the poller; run its pass
                await asyncio.sleep(0.02)
                await state.emitter.check()
                if link.sent:
                    break
            await state.emitter.check()
            assert [meta for _, meta in link.sent] == [{"kind": "channel_check"}]
            token = link.sent[0][0].split("token ")[1].split(".")[0]

            tool = await create_server(state).get_tool("channel_ack")
            assert isinstance(tool, FunctionTool)
            assert await tool.fn(token=token) == "biff channel live."
            try:
                assert await pending.confirmed(2)
            finally:
                await pending.close()
        finally:
            await sub.close()

    async def test_a_second_request_inside_a_minute_sends_no_second_event(
        self, state: ServerState, relay: NatsRelay
    ) -> None:
        link = _Link()
        state.emitter.serve(link)
        state.emitter.initialized()
        sub = ChannelCheckSub(state)
        await sub.start()
        try:
            for _ in range(2):
                pending = await relay.request_channel_check(state.session_key)
                await pending.close()
                await asyncio.sleep(0.2)
                await state.emitter.check()
            assert len(link.sent) == 1
        finally:
            await sub.close()

    async def test_a_forged_request_is_dropped(
        self, state: ServerState, relay: NatsRelay
    ) -> None:
        link = _Link()
        state.emitter.serve(link)
        state.emitter.initialized()
        sub = ChannelCheckSub(state)
        await sub.start()
        try:
            nc = await relay.get_nc()
            subject = relay.channel_check_subject(state.session_key)
            forged = (b"1", b"[]", b'{"reply": 3}', b'{"reply": "biff.x.inbox.kai"}')
            for junk in forged:
                await nc.publish(subject, junk)
            await nc.flush()
            await asyncio.sleep(0.2)
            await state.emitter.check()
            assert link.sent == []
        finally:
            await sub.close()
