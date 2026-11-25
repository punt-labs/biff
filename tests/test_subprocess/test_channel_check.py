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
from mcp.types import JSONRPCResponse

from biff.caller_identity import CALLER_KEY
from biff.nats_relay import NatsRelay
from tests.test_subprocess.test_channel_mode import _Server

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

pytestmark = [pytest.mark.subprocess, pytest.mark.nats_local]

_REPO = "punt-labs__biff"  # what the server derives from this checkout


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


async def test_a_request_makes_one_channel_event_and_the_ack_passes_the_policy(
    tmp_path: Path, nats_server: str, doctor: NatsRelay
) -> None:
    server = _Server(tmp_path, channel_mode=True, relay_url=nats_server)
    try:
        server.initialize()
        # An identified call binds the session, which registers its presence.
        await asyncio.to_thread(
            server.request, "tools/call", {"name": "who", "arguments": {}}
        )
        key = await _session_key(doctor, server.user)

        pending = await doctor.request_channel_check(key)
        try:
            event = await asyncio.to_thread(server.channel_event)
            assert event.params is not None
            assert event.params["meta"] == {"kind": "channel_check"}
            token = re.fullmatch(
                r"biff channel check\. Call channel_ack with token ([0-9a-f]{32})\. "
                r"Do not reply to the user\.",
                str(event.params["content"]),
            )
            assert token is not None

            reply = await asyncio.to_thread(
                server.request,
                "tools/call",
                {"name": "channel_ack", "arguments": {"token": token[1]}},
            )
            assert _text(reply) == "biff channel live."
            assert await pending.confirmed(10)
        finally:
            await pending.close()

        # A second call, stamped the way the plugin's hook stamps it, passes the
        # policy too; the token is spent, so it changes nothing and says so.
        stamp = {"session_id": str(uuid.uuid4()), "agent_id": server.user}
        again = await asyncio.to_thread(
            server.request,
            "tools/call",
            {
                "name": "channel_ack",
                "arguments": {"token": token[1], CALLER_KEY: stamp},
            },
        )
        assert "not accepted" in _text(again)
    finally:
        server.kill()
