"""The channel check through the real ``biff mcp`` stdio process (DES-087).

A doctor-side relay sends a request over a real NATS server.  The server process
answers with exactly one ``notifications/claude/channel`` frame, and the model's
``channel_ack`` call, which goes through the caller policy like every tool call,
replies to the doctor.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import TYPE_CHECKING

import pytest
from mcp.types import JSONRPCNotification, JSONRPCResponse

from biff.caller_identity import CALLER_KEY
from biff.config import get_repo_slug, sanitize_repo_name
from biff.nats_relay import NatsRelay
from tests.test_subprocess.test_channel_mode import _REPO_ROOT, _Server

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from biff.channel_check_pending import PendingCheck

pytestmark = [pytest.mark.subprocess, pytest.mark.nats_local]

# Named exactly as the server names this checkout, so a fork's origin works too.
_REPO = sanitize_repo_name(get_repo_slug(_REPO_ROOT) or _REPO_ROOT.name)


@pytest.fixture
async def doctor(nats_server: str) -> AsyncIterator[NatsRelay]:
    relay = NatsRelay(url=nats_server, repo_name=_REPO)
    yield relay
    await relay.close()


def _text(response: object) -> str:
    assert isinstance(response, JSONRPCResponse)
    return "".join(part.get("text", "") for part in response.result["content"])


async def _session_key(doctor: NatsRelay, user: str) -> str:
    """The key of the session the server registered, as the doctor finds it."""
    for _ in range(100):
        sessions = [s for s in await doctor.get_sessions() if s.user == user]
        if sessions:
            return f"{sessions[0].user}:{sessions[0].tty}"
        await asyncio.sleep(0.1)
    msg = f"no session registered for {user}"
    raise AssertionError(msg)


async def _request_until_heard(
    doctor: NatsRelay, server: _Server, key: str
) -> tuple[list[PendingCheck], JSONRPCNotification]:
    """Send the check until the server's subscription is up and answers.

    Presence is written before the request subscription exists, and core NATS
    does not replay a request nobody heard, so an early one is lost.  Every
    wait stays open: a slow event belongs to whichever request was accepted,
    and its acknowledgement goes to that request's reply inbox.
    """
    pendings: list[PendingCheck] = []
    for _ in range(20):
        pendings.append(await doctor.request_channel_check(key))
        try:
            return pendings, await asyncio.to_thread(server.channel_event, 1.0)
        except AssertionError:
            continue
    for pending in pendings:
        await pending.close()
    msg = "the server never answered a channel check"
    raise AssertionError(msg)


async def test_a_request_makes_one_channel_event_and_the_ack_passes_the_policy(
    tmp_path: Path, nats_server: str, doctor: NatsRelay
) -> None:
    session_id = str(uuid.uuid4())
    server = _Server(
        tmp_path, channel_mode=True, relay_url=nats_server, session_id=session_id
    )
    try:
        server.initialize()
        # An identified call binds the session, which registers its presence.
        await asyncio.to_thread(
            server.request, "tools/call", {"name": "who", "arguments": {}}
        )
        key = await _session_key(doctor, server.user)

        pendings, event = await _request_until_heard(doctor, server, key)
        try:
            assert event.params is not None
            assert event.params["meta"] == {"kind": "channel_check"}
            token = re.fullmatch(
                r"biff channel check\. Call channel_ack with token ([0-9a-f]{32})\. "
                r"Do not reply to the user\.",
                str(event.params["content"]),
            )
            assert token is not None

            # Stamped the way the plugin's hook stamps it, from the active session:
            # the caller policy accepts it and the doctor's wait is confirmed.
            stamp = {"session_id": session_id, "agent_id": server.user}
            reply = await asyncio.to_thread(
                server.request,
                "tools/call",
                {
                    "name": "channel_ack",
                    "arguments": {"token": token[1], CALLER_KEY: stamp},
                },
            )
            assert _text(reply) == "biff channel live."
            confirmed = await asyncio.gather(*(p.confirmed(10) for p in pendings))
            assert sum(confirmed) == 1  # exactly the accepted request's wait
        finally:
            for pending in pendings:
                await pending.close()

        # An unstamped call passes the policy too (the --user default); the
        # token is spent, so it changes nothing and says so.
        again = await asyncio.to_thread(
            server.request,
            "tools/call",
            {"name": "channel_ack", "arguments": {"token": token[1]}},
        )
        assert "not accepted" in _text(again)
    finally:
        server.kill()
