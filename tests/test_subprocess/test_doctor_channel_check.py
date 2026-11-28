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
from biff.plugins.claude_code.channel_check_ask import DaemonAsk
from biff.plugins.claude_code.channel_check_target import CheckTarget
from biff.plugins.claude_code.doctor_checks import DoctorChecks
from tests.test_subprocess.test_channel_check import _REPO, _session_key
from tests.test_subprocess.test_channel_mode import _Server

if TYPE_CHECKING:
    from pathlib import Path

    from biff.plugins.contract import CheckResult

pytestmark = [pytest.mark.subprocess, pytest.mark.nats_local]


class _RelayAsk:
    """Asks over a relay of its own: the server side under test, not the daemon hop."""

    def __init__(self, url: str) -> None:
        self._url = url

    async def confirmed(self, session_key: str, seconds: float) -> bool:
        relay = NatsRelay(url=self._url, repo_name=_REPO)
        try:
            pending = await relay.request_channel_check(session_key)
            try:
                return await pending.confirmed(seconds)
            finally:
                await pending.close()
        finally:
            await relay.close()


def _ask_over(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make doctor's default ask a relay of its own, as no biffd runs here."""

    def relay_ask(_user: str | None) -> _RelayAsk:
        return _RelayAsk(url)

    monkeypatch.setattr(DaemonAsk, "for_this_process", staticmethod(relay_ask))


async def _registered(url: str, user: str) -> None:
    relay = NatsRelay(url=url, repo_name=_REPO)
    try:
        await _session_key(relay, user)
    finally:
        await relay.close()


def test_doctor_reports_channel_live_for_a_session_that_acknowledges(
    tmp_path: Path, nats_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ask_over(nats_server, monkeypatch)
    server = _Server(tmp_path, channel_mode=True, relay_url=nats_server)
    try:
        server.initialize()
        server.request("tools/call", {"name": "who", "arguments": {}})
        asyncio.run(_registered(nats_server, server.user))

        def target(_override: str | None) -> CheckTarget:
            relay = NatsRelay(url=nats_server, repo_name=_REPO)
            return CheckTarget(relay, (server.user,))

        results: list[CheckResult] = []
        doctor = threading.Thread(
            target=lambda: results.extend(
                DoctorChecks(
                    tmp_path / "settings.json", {}, None, target
                ).channel_check()
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


def test_doctor_reports_not_confirmed_when_channels_are_off(
    tmp_path: Path, nats_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server without channel mode cannot send the event: doctor says so."""
    _ask_over(nats_server, monkeypatch)
    server = _Server(tmp_path, channel_mode=False, relay_url=nats_server)
    try:
        server.initialize()
        server.request("tools/call", {"name": "who", "arguments": {}})
        asyncio.run(_registered(nats_server, server.user))

        def target(_override: str | None) -> CheckTarget:
            relay = NatsRelay(url=nats_server, repo_name=_REPO)
            return CheckTarget(relay, (server.user,))

        (line,) = DoctorChecks(
            tmp_path / "settings.json", {}, None, target
        ).channel_check()

        assert not line.passed
        assert not line.required  # doctor never fails on a channel
        assert line.message.startswith(f"{server.user}:")
        assert ": channel not confirmed." in line.message
        assert "Channels lines above" in line.message
    finally:
        server.kill()
