"""Docker demo: push beats poll for broadcast message detection (biff-5ex).

Runs the same A/B measurement as ``tests/test_nats_e2e/test_push_vs_poll_latency.py``
end to end against the real ``ghcr.io/punt-labs/biff-relay`` container built
fresh from ``docker/`` — ``entrypoint.sh``'s config selection, the real
auth handshake, and the container network, not a bare ``nats-server``.
The printed comparison table plus the saved transcript (this test is marked
``@pytest.mark.transcript``) are the demo artifact ``scripts/demo-push-vs-poll.sh``
points an operator at.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig, Message, RelayAuth
from biff.nats_relay import NatsRelay
from biff.server.app import create_server
from biff.server.state import create_state
from biff.server.tools._descriptions import _reset_session, nap_interval_for
from biff.testing import NotificationTracker, RecordingClient, Transcript

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ._docker import RelayContainer

pytestmark = pytest.mark.nats_docker

_TEST_REPO = "_test-push-vs-poll-demo"
_KAI_TTY = "aaaa00d1"

# Fast enough to wait out in a test, mirroring test_push_vs_poll_latency.py.
_POLL_INTERVAL = 0.3
_BACKSTOP = nap_interval_for(_POLL_INTERVAL)
_TRIAL_TIMEOUT = _BACKSTOP * 2.0
_SYNC_PAUSE = _BACKSTOP + 2 * _POLL_INTERVAL

_TRANSCRIPT_DIR = Path(__file__).parent.parent / "transcripts"


@pytest.fixture
def transcript(request: pytest.FixtureRequest) -> Iterator[Transcript]:
    """Save a human-readable transcript for ``@pytest.mark.transcript`` tests.

    This tier's own ``conftest.py`` carries no ``transcript`` fixture (it's
    scoped to Docker orchestration only), so this demo test provides its
    own — mirroring ``tests/test_nats_e2e/conftest.py``'s fixture exactly.
    """
    t = Transcript(title="biff-5ex: push beats poll for broadcast detection")
    yield t
    node = cast("pytest.Item", request.node)  # pyright: ignore[reportUnknownMemberType]
    marker = node.get_closest_marker("transcript")
    if marker and t.entries:
        _TRANSCRIPT_DIR.mkdir(exist_ok=True)
        slug = node.name.replace("[", "_").replace("]", "")
        path = _TRANSCRIPT_DIR / f"{slug}.txt"
        path.write_text(t.render())


def _client_config(
    container: RelayContainer, user: str, *, poll_interval: float
) -> BiffConfig:
    return BiffConfig(
        user=user,
        repo_name=_TEST_REPO,
        relay_url=container.url,
        relay_auth=RelayAuth(token=container.token),
        poll_interval=poll_interval,
    )


async def _publish_broadcast_without_poke(
    sender: NatsRelay, *, to_user: str, from_user: str, body: str
) -> None:
    """Publish straight to JetStream, bypassing ``deliver()``'s wake poke.

    Uses ``sender``'s own already-authenticated JetStream context and
    subject-naming method (private, white-box access — mirrors
    ``test_inbox_push.py``'s ``TestPokeSubjectDoesNotCollideWithInboxStream``)
    instead of a second bare connection, so this helper authenticates
    against the real container the same way ``deliver()`` itself does,
    without duplicating the token wiring here.
    """
    js, _ = await sender._ensure_connected()  # white-box bypass, see docstring
    msg = Message(from_user=from_user, to_user=to_user, body=body)
    subject = sender._user_subject(to_user)  # white-box bypass, see docstring
    await js.publish(  # pyright: ignore[reportUnknownMemberType]
        subject,
        msg.model_dump_json().encode(),
        headers={"Nats-Msg-Id": str(uuid.uuid4())},
    )


async def _wait_for_unread_description(
    client: Client[Any], *, timeout: float
) -> float | None:
    """Poll ``list_tools()`` until ``read_messages`` shows "1 unread"."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        tools = await client.list_tools()
        for tool in tools:
            if tool.name == "read_messages" and "1 unread" in (tool.description or ""):
                return time.monotonic()
        await asyncio.sleep(0.1)
    return None


def _print_comparison(push_latency: float, poll_latency: float) -> None:
    print(f"\n{'Path':<8} {'Latency (ms)':>14}")
    print("-" * 24)
    print(f"{'push':<8} {push_latency * 1000:>14.1f}")
    print(f"{'poll':<8} {poll_latency * 1000:>14.1f}")
    print(f"\nbackstop (nap_interval): {_BACKSTOP * 1000:.1f}ms")


class TestPushVsPollDemo:
    """The push-vs-poll demonstration against the real relay container."""

    @pytest.mark.transcript
    async def test_push_beats_poll_against_real_relay(
        self,
        relay_container: RelayContainer,
        tmp_path: Path,
        transcript: Transcript,
    ) -> None:
        """Push detects a broadcast fast; poll waits out the backstop cadence."""
        _reset_session()
        kai_config = _client_config(
            relay_container, "kai", poll_interval=_POLL_INTERVAL
        )
        kai_state = create_state(
            kai_config,
            tmp_path / "kai",
            tty=_KAI_TTY,
            hostname="test-host",
            pwd="/test",
        )
        mcp = create_server(kai_state)
        tracker = NotificationTracker()
        sender = NatsRelay(
            url=relay_container.url,
            auth=RelayAuth(token=relay_container.token),
            repo_name=_TEST_REPO,
        )
        try:
            async with Client(
                FastMCPTransport(mcp), message_handler=tracker
            ) as kai_raw:
                kai = RecordingClient(client=kai_raw, transcript=transcript, user="kai")
                await asyncio.sleep(1.0)  # let the inbox-notify SUB get established

                await kai.call("read_messages")  # baseline: "No new messages."

                # -- push: real deliver(), JetStream + inbox-notify poke --
                t_send = time.monotonic()
                await sender.deliver(
                    Message(
                        from_user="eric", to_user="kai", body="standup notes (push)"
                    )
                )
                t_recv = await _wait_for_unread_description(
                    kai_raw, timeout=_TRIAL_TIMEOUT
                )
                assert t_recv is not None, "push: read_messages never showed 1 unread"
                push_latency = t_recv - t_send
                await kai.call("read_messages")  # drains, records the delivered text

                # -- poll: JetStream publish only, no poke (pre-biff-5ex shape) --
                await asyncio.sleep(_SYNC_PAUSE)  # let the backstop clock reset first
                t_send = time.monotonic()
                await _publish_broadcast_without_poke(
                    sender, to_user="kai", from_user="eric", body="standup notes (poll)"
                )
                t_recv = await _wait_for_unread_description(
                    kai_raw, timeout=_TRIAL_TIMEOUT
                )
                assert t_recv is not None, "poll: read_messages never showed 1 unread"
                poll_latency = t_recv - t_send
                await kai.call("read_messages")  # drains, records the delivered text

            _print_comparison(push_latency, poll_latency)

            # Generous, CI-stable margins — same bounds as the tier-3c
            # comparative test, since the docker tier runs in CI on
            # every PR (TESTING.md).
            assert push_latency < 2.0, (
                f"push took {push_latency * 1000:.1f}ms, expected < 2s"
            )
            assert poll_latency >= _BACKSTOP * 0.5, (
                f"poll took {poll_latency * 1000:.1f}ms, "
                f"expected >= {_BACKSTOP * 500:.1f}ms (half the backstop)"
            )
        finally:
            await sender.close()
            _reset_session()
