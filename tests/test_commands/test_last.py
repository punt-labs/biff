"""Tests for ``biff.commands.last``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from biff.cli_session import CliContext
from biff.commands.last import last
from biff.models import BiffConfig, SessionEvent, UserSession

from ._relay import WtmpRelay


def _make_ctx(relay: WtmpRelay) -> CliContext:
    return CliContext(
        relay=relay,
        config=BiffConfig(user="kai", repo_name="test"),
        session_key="kai:abc12345",
        user="kai",
        tty="abc12345",
    )


class TestLast:
    async def test_empty_history(self, ctx: CliContext) -> None:
        result = await last(ctx, "", 25)
        assert not result.error
        assert result.text == "No session history."
        assert result.json_data == []

    async def test_count_clamped_high(self, ctx: CliContext) -> None:
        result = await last(ctx, "", 999)
        assert not result.error
        assert result.json_data == []

    async def test_count_clamped_low(self, ctx: CliContext) -> None:
        result = await last(ctx, "", 0)
        assert not result.error
        assert result.json_data == []


class TestLastWithEvents:
    async def test_login_event(self, tmp_path: Path) -> None:
        relay = WtmpRelay(tmp_path)
        ctx = _make_ctx(relay)
        now = datetime.now(UTC)

        await relay.append_wtmp(
            SessionEvent(
                session_key="eric:def67890",
                event="login",
                user="eric",
                tty="def67890",
                tty_name="tty1",
                hostname="laptop",
                timestamp=now - timedelta(hours=1),
            )
        )

        result = await last(ctx, "", 25)
        assert not result.error
        assert "@eric" in result.text
        data = cast("list[dict[str, object]]", result.json_data)
        assert len(data) == 1
        assert data[0]["user"] == "eric"
        assert data[0]["tty"] == "tty1"
        assert data[0]["logout"] is None
        assert data[0]["active"] is False

    async def test_login_logout_pair(self, tmp_path: Path) -> None:
        relay = WtmpRelay(tmp_path)
        ctx = _make_ctx(relay)
        now = datetime.now(UTC)

        await relay.append_wtmp(
            SessionEvent(
                session_key="kai:abc12345",
                event="login",
                user="kai",
                tty="abc12345",
                tty_name="dev",
                timestamp=now - timedelta(hours=2),
            )
        )
        await relay.append_wtmp(
            SessionEvent(
                session_key="kai:abc12345",
                event="logout",
                user="kai",
                tty="abc12345",
                tty_name="dev",
                timestamp=now - timedelta(hours=1),
            )
        )

        result = await last(ctx, "", 25)
        assert not result.error
        data = cast("list[dict[str, object]]", result.json_data)
        assert len(data) == 1
        assert data[0]["user"] == "kai"
        assert data[0]["logout"] is not None

    async def test_active_session_marked(self, tmp_path: Path) -> None:
        relay = WtmpRelay(tmp_path)
        ctx = _make_ctx(relay)
        now = datetime.now(UTC)

        # Register an active session
        await relay.update_session(
            UserSession(user="kai", tty="abc12345", tty_name="dev")
        )
        # Add login event for that session
        await relay.append_wtmp(
            SessionEvent(
                session_key="kai:abc12345",
                event="login",
                user="kai",
                tty="abc12345",
                tty_name="dev",
                timestamp=now - timedelta(minutes=30),
            )
        )

        result = await last(ctx, "", 25)
        data = cast("list[dict[str, object]]", result.json_data)
        assert len(data) == 1
        assert data[0]["active"] is True

    async def test_filter_by_user(self, tmp_path: Path) -> None:
        relay = WtmpRelay(tmp_path)
        ctx = _make_ctx(relay)
        now = datetime.now(UTC)

        await relay.append_wtmp(
            SessionEvent(
                session_key="kai:abc12345",
                event="login",
                user="kai",
                tty="abc12345",
                timestamp=now - timedelta(hours=1),
            )
        )
        await relay.append_wtmp(
            SessionEvent(
                session_key="eric:def67890",
                event="login",
                user="eric",
                tty="def67890",
                timestamp=now,
            )
        )

        # Filter to kai only
        result = await last(ctx, "@kai", 25)
        assert not result.error
        data = cast("list[dict[str, object]]", result.json_data)
        assert len(data) == 1
        assert data[0]["user"] == "kai"

    async def test_filter_strips_at(self, tmp_path: Path) -> None:
        relay = WtmpRelay(tmp_path)
        ctx = _make_ctx(relay)
        await relay.append_wtmp(
            SessionEvent(
                session_key="kai:abc12345",
                event="login",
                user="kai",
                tty="abc12345",
                timestamp=datetime.now(UTC),
            )
        )
        # With @ prefix
        result = await last(ctx, "@kai", 25)
        data = cast("list[dict[str, object]]", result.json_data)
        assert len(data) == 1

    async def test_count_limits_output(self, tmp_path: Path) -> None:
        relay = WtmpRelay(tmp_path)
        ctx = _make_ctx(relay)
        now = datetime.now(UTC)

        for i in range(5):
            await relay.append_wtmp(
                SessionEvent(
                    session_key=f"kai:tty{i:05d}",
                    event="login",
                    user="kai",
                    tty=f"tty{i:05d}",
                    timestamp=now - timedelta(hours=i),
                )
            )

        result = await last(ctx, "", 3)
        data = cast("list[dict[str, object]]", result.json_data)
        assert len(data) == 3

    async def test_multiple_users(self, tmp_path: Path) -> None:
        relay = WtmpRelay(tmp_path)
        ctx = _make_ctx(relay)
        now = datetime.now(UTC)

        await relay.append_wtmp(
            SessionEvent(
                session_key="kai:aaa11111",
                event="login",
                user="kai",
                tty="aaa11111",
                tty_name="dev",
                timestamp=now - timedelta(hours=2),
            )
        )
        await relay.append_wtmp(
            SessionEvent(
                session_key="eric:bbb22222",
                event="login",
                user="eric",
                tty="bbb22222",
                tty_name="review",
                timestamp=now - timedelta(hours=1),
            )
        )

        result = await last(ctx, "", 25)
        data = cast("list[dict[str, object]]", result.json_data)
        assert len(data) == 2
        users = {d["user"] for d in data}
        assert users == {"kai", "eric"}
