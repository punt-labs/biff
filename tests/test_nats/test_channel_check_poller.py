"""The poller's always-on subscriptions include the channel check request (DES-087)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from biff.models import BiffConfig
from biff.server.tools._descriptions import (
    _AlwaysOnSubs,  # pyright: ignore[reportPrivateUsage]
    _InboxPokeGate,  # pyright: ignore[reportPrivateUsage]
)
from tests._state import create_state

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from biff.nats_relay import NatsRelay

pytestmark = pytest.mark.nats_local


class _Link:
    is_open = True

    def __init__(self) -> None:
        self.metas: list[dict[str, str]] = []

    async def send(self, content: str, meta: Mapping[str, str]) -> None:
        self.metas.append(dict(meta))


async def test_the_pollers_subscriptions_receive_a_check_request(
    tmp_path: Path, relay: NatsRelay
) -> None:
    config = BiffConfig(user="kai", repo_name="_test-nats-unit")
    state = create_state(
        config, tmp_path, tty="tty1", hostname="h", pwd="/p", repo_root=tmp_path
    )
    object.__setattr__(state, "relay", relay)
    link = _Link()
    state.emitter.serve(link)
    state.emitter.initialized()
    subs = _AlwaysOnSubs(state, _InboxPokeGate())
    await subs.start()
    try:
        pending = await relay.request_channel_check(state.session_key)
        await pending.close()
        await asyncio.sleep(0.2)
        await state.emitter.check()
        assert link.metas == [{"kind": "channel_check"}]
    finally:
        await subs.close()
