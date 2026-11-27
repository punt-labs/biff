"""``biff.commands.status`` reports a relay's request and timeout counters."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

from biff.commands.status import status
from biff.relay import LocalRelay

if TYPE_CHECKING:
    from pathlib import Path

    from biff.cli_context import CliContext


class CountingLocalRelay(LocalRelay):
    """A local relay that counts requests the way the NATS relay does."""

    @property
    def total_attempts(self) -> int:
        return 7

    @property
    def total_timeouts(self) -> int:
        return 2


async def test_a_local_relay_has_no_counters(ctx: CliContext) -> None:
    data = cast("dict[str, object]", (await status(ctx)).json_data)
    assert data["relay_counters"] is None


async def test_a_counting_relay_reports_attempts_and_timeouts(
    ctx: CliContext, tmp_path: Path
) -> None:
    counting = replace(ctx, relay=CountingLocalRelay(tmp_path))
    data = cast("dict[str, object]", (await status(counting)).json_data)
    assert data["relay_counters"] == {"attempts": 7, "timeouts": 2}


async def test_the_status_text_is_unchanged_by_counters(
    ctx: CliContext, tmp_path: Path
) -> None:
    counting = replace(ctx, relay=CountingLocalRelay(tmp_path))
    assert (await status(counting)).text == (await status(ctx)).text
