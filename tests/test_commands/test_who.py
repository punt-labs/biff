"""Tests for ``biff.commands.who``."""

from __future__ import annotations

from typing import cast

from biff.cli_session import CliContext
from biff.commands.who import who
from biff.models import UserSession
from biff.relay import LocalRelay


class TestWho:
    async def test_empty(self, ctx: CliContext) -> None:
        result = await who(ctx)
        assert result.text == "No sessions."
        assert result.json_data == []
        assert not result.error

    async def test_one_session(self, ctx: CliContext, relay: LocalRelay) -> None:
        await relay.update_session(
            UserSession(user="kai", tty="abc12345", tty_name="tty1", plan="coding")
        )
        result = await who(ctx)
        assert "kai" in result.text
        assert not result.error
        data = cast("list[dict[str, object]]", result.json_data)
        assert len(data) == 1

    async def test_multiple_sessions_sorted(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        # Both within the liveness window (< 120s) so both are shown.
        await relay.update_session(
            UserSession(
                user="kai",
                tty="aaa11111",
                tty_name="tty1",
                last_active=now - timedelta(seconds=30),
            )
        )
        await relay.update_session(
            UserSession(
                user="eric",
                tty="bbb22222",
                tty_name="tty2",
                last_active=now,
            )
        )
        result = await who(ctx)
        assert not result.error
        data = cast("list[dict[str, object]]", result.json_data)
        assert len(data) == 2
        # Most recently active first
        assert data[0]["user"] == "eric"
        assert data[1]["user"] == "kai"

    async def test_row_order_follows_last_tool_at_not_last_active(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        """Row order must match the IDLE column (last_tool_at), not the
        heartbeat-refreshed last_active -- otherwise the visible order
        and the visible idle values disagree."""
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        # kai heartbeated most recently but has been idle the longest;
        # eric is the opposite. Sorting by last_active would put kai
        # first; sorting by last_tool_at (matching IDLE) puts eric first.
        await relay.update_session(
            UserSession(
                user="kai",
                tty="aaa11111",
                tty_name="tty1",
                last_active=now,
                last_tool_at=now - timedelta(minutes=10),
            )
        )
        await relay.update_session(
            UserSession(
                user="eric",
                tty="bbb22222",
                tty_name="tty2",
                last_active=now - timedelta(seconds=5),
                last_tool_at=now,
            )
        )
        result = await who(ctx)
        assert not result.error
        data = cast("list[dict[str, object]]", result.json_data)
        assert data[0]["user"] == "eric"
        assert data[1]["user"] == "kai"

    async def test_hides_dead_session(self, ctx: CliContext, relay: LocalRelay) -> None:
        """A session that stopped heartbeating (> liveness window) is hidden."""
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        await relay.update_session(
            UserSession(user="live", tty="aaa11111", tty_name="tty1", last_active=now)
        )
        await relay.update_session(
            UserSession(
                user="dead",
                tty="bbb22222",
                tty_name="tty2",
                last_active=now - timedelta(hours=5),
            )
        )
        result = await who(ctx)
        assert not result.error
        assert "live" in result.text
        assert "dead" not in result.text
        data = cast("list[dict[str, object]]", result.json_data)
        assert len(data) == 1
        assert data[0]["user"] == "live"

    async def test_all_dead_returns_no_sessions(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        """When every session is stale, the main table reports no sessions,
        but a session dead long enough is still named-free in the footnote
        (DES-056) -- the exact orphan case that motivated this bead."""
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        await relay.update_session(
            UserSession(
                user="ghost",
                tty="ccc33333",
                tty_name="tty1",
                last_active=now - timedelta(hours=12),
            )
        )
        result = await who(ctx)
        assert result.text == (
            "No sessions.\n   1 session stopped responding (last seen 12h)"
        )
        assert "ghost" not in result.text
        assert result.json_data == []
