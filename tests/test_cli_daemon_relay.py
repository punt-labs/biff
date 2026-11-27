"""The relay line asks the daemon from sync code, even inside a running loop."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.cli_daemon_relay import RelayStatus

if TYPE_CHECKING:
    import pytest


async def _reply(port: int) -> str:
    return f"relay: local:{port}"


def test_status_works_from_plain_sync_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RelayStatus, "_ask", staticmethod(_reply))
    assert RelayStatus().status(7) == "relay: local:7"


async def test_status_does_not_raise_inside_a_running_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(RelayStatus, "_ask", staticmethod(_reply))
    assert RelayStatus().status(7) == "relay: local:7"
