"""The ``status`` method reports the relay's request and timeout counters."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from biff.relay import LocalRelay

if TYPE_CHECKING:
    from pathlib import Path

    from biff.relay import Relay
    from tests.test_daemon.conftest import Harness


class _CountingRelay(LocalRelay):
    @property
    def total_attempts(self) -> int:
        return 9

    @property
    def total_timeouts(self) -> int:
        return 3


async def test_status_carries_the_counters_of_a_counting_relay(
    harness: Harness,
) -> None:
    def build(path: Path) -> Relay:
        return _CountingRelay(path)

    harness.factory.relay_type = build
    mcp = await harness.connect("s-kai", "kai", client="mcp")
    reply = cast("dict[str, dict[str, object]]", await mcp.request("status"))
    assert reply["data"]["relay_counters"] == {"attempts": 9, "timeouts": 3}


async def test_status_has_no_counters_for_a_local_relay(harness: Harness) -> None:
    mcp = await harness.connect("s-kai", "kai", client="mcp")
    reply = cast("dict[str, dict[str, object]]", await mcp.request("status"))
    assert reply["data"]["relay_counters"] is None
