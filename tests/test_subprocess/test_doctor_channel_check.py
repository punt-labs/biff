"""``biff doctor``'s channel check against a real channel-mode ``biff mcp`` (DES-087).

The server runs as a subprocess over a real NATS server.  Doctor's check sends
its request, the server sends one channel event, the test plays the model by
calling ``channel_ack`` from this thread, and the doctor line reads
``channel live``.
"""

from __future__ import annotations

import asyncio
import re
import threading
from typing import TYPE_CHECKING

import pytest

from biff.nats_relay import NatsRelay
from biff.plugins.claude_code.channel_check_report import CheckTarget
from biff.plugins.claude_code.doctor_checks import DoctorChecks
from tests.test_subprocess.test_channel_check import _REPO, _session_key
from tests.test_subprocess.test_channel_mode import _Server

if TYPE_CHECKING:
    from pathlib import Path

    from biff.plugins.contract import CheckResult

pytestmark = [pytest.mark.subprocess, pytest.mark.nats_local]


async def _registered(url: str, user: str) -> None:
    relay = NatsRelay(url=url, repo_name=_REPO)
    try:
        await _session_key(relay, user)
    finally:
        await relay.close()


def test_doctor_reports_channel_live_for_a_session_that_acknowledges(
    tmp_path: Path, nats_server: str
) -> None:
    server = _Server(tmp_path, channel_mode=True, relay_url=nats_server)
    try:
        server.initialize()
        server.request("tools/call", {"name": "who", "arguments": {}})
        asyncio.run(_registered(nats_server, server.user))

        def target() -> CheckTarget:
            relay = NatsRelay(url=nats_server, repo_name=_REPO)
            return CheckTarget(relay, server.user)

        results: list[CheckResult] = []
        doctor = threading.Thread(
            target=lambda: results.extend(
                DoctorChecks(tmp_path / "settings.json", {}, target).channel_check()
            )
        )
        doctor.start()

        event = server.channel_event()
        assert event.params is not None
        token = re.search(r"token ([0-9a-f]{32})\.", str(event.params["content"]))
        assert token is not None
        server.request(
            "tools/call", {"name": "channel_ack", "arguments": {"token": token[1]}}
        )
        doctor.join(timeout=30)

        (line,) = results
        assert line.passed
        assert not line.required
        assert line.message.endswith(": channel live")
        assert line.message.startswith(f"{server.user}:")
    finally:
        server.kill()
