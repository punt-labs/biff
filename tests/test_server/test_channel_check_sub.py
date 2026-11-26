"""A check request reaches the shared check state and wakes the poller (DES-087)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from biff.channel_check_wire import CheckRequest
from biff.models import BiffConfig
from biff.server.channel_check_sub import ChannelCheckSub
from tests._state import create_state

if TYPE_CHECKING:
    from pathlib import Path

    from nats.aio.msg import Msg

    from biff.server.state import ServerState

_REPLY = "_INBOX.doctor1"


class _Link:
    is_open = True

    async def send(self, content: str, meta: object) -> None:
        return None


def _msg(data: bytes) -> Msg:
    return cast("Msg", SimpleNamespace(data=data))


@pytest.fixture
def state(tmp_path: Path) -> ServerState:
    return create_state(BiffConfig(user="kai", repo_name="r"), tmp_path, tty="tty1")


async def test_a_request_is_accepted_and_wakes_the_poller(state: ServerState) -> None:
    state.emitter.serve(_Link())
    sub = ChannelCheckSub(state)

    await sub.handle_request(_msg(CheckRequest(_REPLY).to_payload()))

    assert state.channel_check.take_unsent() is not None
    assert state.wake.is_set


async def test_with_channel_mode_off_a_request_is_ignored(state: ServerState) -> None:
    sub = ChannelCheckSub(state)

    await sub.handle_request(_msg(CheckRequest(_REPLY).to_payload()))

    assert state.channel_check.take_unsent() is None
