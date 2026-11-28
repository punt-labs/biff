"""``biff doctor`` asks a session to prove its channel through biffd.

The doctor holds no relay for the check itself: ``DaemonAsk`` sends the
daemon's ``channel_check`` method and reads whether the session acknowledged.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest

from biff.client.connection import DaemonUnreachableError
from biff.models import UserSession
from biff.plugins.claude_code.channel_check_ask import CheckNotSentError, DaemonAsk
from biff.plugins.claude_code.channel_check_report import ChannelCheckReport
from biff.plugins.claude_code.channel_check_target import CheckTarget
from tests.test_daemon.push_relay import PushRelay
from tests.test_daemon.talk_bus import TalkBus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from biff.client.connection import DaemonClient
    from biff.frames import Event
    from tests.test_daemon.conftest import Harness

_SOON = 2.0


@pytest.fixture
async def bus(harness: Harness, tmp_path: Path) -> AsyncIterator[TalkBus]:
    shared = TalkBus()

    def build(path: Path) -> PushRelay:
        return PushRelay(path, shared)

    harness.factory.relay_type = build
    yield shared


def _ask(harness: Harness, repo: Path, request_timeout: float = 5.0) -> DaemonAsk:
    return DaemonAsk(
        user="kai",
        run_dir=harness.run_dir,
        nudge=lambda: harness.daemon.port,
        cwd=str(repo),
        request_timeout=request_timeout,
    )


async def _session(harness: Harness, bus: TalkBus) -> tuple[DaemonClient, str]:
    client = await harness.connect("s-kai", "kai", client="mcp")
    status = cast("dict[str, dict[str, str]]", await client.request("status"))
    key = status["data"]["session_key"]
    await client.subscribe()
    await asyncio.wait_for(client.next_event(), _SOON)  # the unread summary
    await bus.flush()
    return client, key


def _token(event: Event) -> str:
    assert event.kind == "channel_check_request"
    return cast("dict[str, str]", event.data)["token"]


async def test_a_session_that_acknowledges_is_confirmed(
    harness: Harness, bus: TalkBus, repo: Path
) -> None:
    kai, key = await _session(harness, bus)
    asked = asyncio.ensure_future(_ask(harness, repo).confirmed(key, 5))
    token = _token(await asyncio.wait_for(kai.next_event(), _SOON))
    await kai.request("channel_ack", token=token)
    assert await asked is True


async def test_a_session_that_stays_silent_is_not_confirmed(
    harness: Harness, bus: TalkBus, repo: Path
) -> None:
    _, key = await _session(harness, bus)
    assert await _ask(harness, repo).confirmed(key, 0) is False


async def test_the_ask_outwaits_an_ordinary_request_timeout(
    harness: Harness, bus: TalkBus, repo: Path
) -> None:
    """An ordinary request here gives up at 0.2 s; the acknowledgement takes 0.35 s."""
    kai, key = await _session(harness, bus)
    asked = asyncio.ensure_future(_ask(harness, repo, 0.2).confirmed(key, 5))
    token = _token(await asyncio.wait_for(kai.next_event(), _SOON))
    await asyncio.sleep(0.35)
    await kai.request("channel_ack", token=token)
    assert await asked is True


async def test_the_ask_stores_no_session_record(
    harness: Harness, bus: TalkBus, repo: Path
) -> None:
    _, key = await _session(harness, bus)
    await _ask(harness, repo).confirmed(key, 0)
    assert harness.sessions.list() == ()


async def test_a_local_relay_is_a_check_that_was_not_sent(
    harness: Harness, repo: Path
) -> None:
    with pytest.raises(CheckNotSentError, match="NATS"):
        await _ask(harness, repo).confirmed("kai:abc", 0)


async def test_a_daemon_that_cannot_be_reached_is_a_check_that_was_not_sent(
    harness: Harness, repo: Path
) -> None:
    def down() -> int:
        raise DaemonUnreachableError("biffd is not answering")

    ask = DaemonAsk(user="kai", run_dir=harness.run_dir, nudge=down, cwd=str(repo))
    with pytest.raises(CheckNotSentError, match="not answering"):
        await ask.confirmed("kai:abc", 0)


async def test_a_reason_biffd_gives_is_stripped_of_escapes(
    harness: Harness, repo: Path
) -> None:
    def down() -> int:
        raise DaemonUnreachableError("bad \x1b[31mnews")

    ask = DaemonAsk(user="kai", run_dir=harness.run_dir, nudge=down, cwd=str(repo))
    with pytest.raises(CheckNotSentError) as seen:
        await ask.confirmed("kai:abc", 0)
    assert "\x1b" not in str(seen.value)


async def test_a_float_wait_reaches_the_session(
    harness: Harness, bus: TalkBus, repo: Path
) -> None:
    """The doctor's wait is a float; the daemon method takes whole seconds."""
    kai, key = await _session(harness, bus)
    asked = asyncio.ensure_future(_ask(harness, repo).confirmed(key, 5.0))
    token = _token(await asyncio.wait_for(kai.next_event(), _SOON))
    await kai.request("channel_ack", token=token)
    assert await asked is True


class _OneSession:
    """The doctor's presence lookup: one live session, whatever the handle."""

    def __init__(self, key: str) -> None:
        user, tty = key.split(":")
        self._session = UserSession(user=user, tty=tty, last_active=datetime.now(UTC))

    async def list_sessions_for_user(self, user: str) -> list[UserSession]:
        return [self._session]

    async def close(self) -> None:
        return None


async def test_the_doctor_with_its_default_wait_reaches_a_real_session(
    harness: Harness, bus: TalkBus, repo: Path
) -> None:
    """ChannelCheckReport's default wait and the real DaemonAsk, end to end."""
    kai, key = await _session(harness, bus)
    report = ChannelCheckReport(
        CheckTarget(_OneSession(key), ("kai",)), ask=lambda: _ask(harness, repo)
    )
    lines = asyncio.ensure_future(report.results())
    token = _token(await asyncio.wait_for(kai.next_event(), _SOON))
    await kai.request("channel_ack", token=token)
    (line,) = await lines
    assert line.message.endswith("channel live")
