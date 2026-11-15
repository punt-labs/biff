"""Tests for ``biff.commands.tty``."""

from __future__ import annotations

from pathlib import Path

import pytest

from biff.cli_context import CliContext
from biff.commands.tty import tty
from biff.models import BiffConfig, UserSession
from biff.relay import LocalRelay


class TestTty:
    async def test_explicit_name(self, ctx: CliContext, relay: LocalRelay) -> None:
        result = await tty(ctx, "dev")
        assert not result.error
        assert result.text == "TTY: dev"
        assert result.json_data == {"tty": "dev"}

        session = await relay.get_session("kai:abc12345")
        assert session is not None
        assert session.tty_name == "dev"

    async def test_auto_name(self, ctx: CliContext) -> None:
        result = await tty(ctx, "")
        assert not result.error
        assert result.text == "TTY: tty1"
        assert result.json_data == {"tty": "tty1"}

    async def test_auto_name_increments(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await relay.update_session(
            UserSession(user="kai", tty="eee11111", tty_name="tty1")
        )
        # Reserve the name so claim_tty_name sees it (DES-035).
        await relay.reserve_tty_name("kai", "tty1", "kai:eee11111")
        result = await tty(ctx, "")
        assert result.text == "TTY: tty2"

    async def test_name_too_long(self, ctx: CliContext) -> None:
        result = await tty(ctx, "a" * 21)
        assert result.error
        assert "Invalid tty name" in result.text

    async def test_name_collision(self, ctx: CliContext, relay: LocalRelay) -> None:
        # Another session owned by kai with name "dev" — reserve via relay (DES-035).
        await relay.update_session(
            UserSession(user="kai", tty="other123", tty_name="dev")
        )
        await relay.reserve_tty_name("kai", "dev", "kai:other123")
        result = await tty(ctx, "dev")
        assert result.error
        assert "already in use" in result.text

    async def test_same_name_on_different_user_ok(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await relay.update_session(
            UserSession(user="eric", tty="eee11111", tty_name="dev")
        )
        result = await tty(ctx, "dev")
        assert not result.error
        assert result.text == "TTY: dev"

    async def test_name_exactly_20_chars(self, ctx: CliContext) -> None:
        name = "a" * 20
        result = await tty(ctx, name)
        assert not result.error
        assert result.json_data == {"tty": name}

    async def test_rename_existing_session(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await relay.update_session(
            UserSession(user="kai", tty="abc12345", tty_name="old")
        )
        result = await tty(ctx, "new")
        assert not result.error
        assert result.text == "TTY: new"

        session = await relay.get_session("kai:abc12345")
        assert session is not None
        assert session.tty_name == "new"

    async def test_whitespace_only_name_gets_auto(self, ctx: CliContext) -> None:
        result = await tty(ctx, "   ")
        assert not result.error
        # Whitespace-only should be treated as empty → auto-naming
        assert "tty" in result.text

    async def test_rename_updates_reclaim_hint(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        """A rename refreshes the session_id->ttyN hint so the next resume
        reclaims the RENAMED alias, not the stale one."""
        result = await tty(ctx, "deploy")
        assert not result.error
        assert await relay.get_session_tty_hint(ctx.user, ctx.tty) == "deploy"


class TestRenameKeepsTheStoredRecordInSync:
    async def test_rename_updates_the_record_the_next_attach_reads(
        self, tmp_path: Path
    ) -> None:
        from biff.cli_session_host import SessionHost
        from biff.cli_session_pointer import CurrentPointer
        from biff.cli_session_select import SessionSelector
        from biff.cli_session_store import CliSessionStore
        from biff.models import BiffConfig

        root = tmp_path / "repo"
        root.mkdir()
        host = SessionHost(
            relay=LocalRelay(tmp_path / "relay"),
            config=BiffConfig(user="kai", repo_name="biff"),
            selector=SessionSelector(
                store=CliSessionStore(tmp_path / "sessions"),
                pointer=CurrentPointer(tmp_path / "current"),
                user="kai",
                repo="biff",
                repo_root=root,
                hostname="h1",
            ),
            hostname="h1",
            pwd=str(root),
        )
        attached = await host.resolve(None)
        assert attached.record is not None
        ctx = host.context_for(attached)

        result = await tty(ctx, "deploy")

        assert result.text == "TTY: deploy"
        record = host.selector.store.read(attached.record.session_id)
        assert record.tty == "deploy"
        # The next bare invocation keeps the renamed address; it does not
        # restore the old name or release the new reservation.
        again = await host.resolve(None)
        assert again.tty_name == "deploy"
        key = ctx.session_key
        assert await host.relay.get_tty_reservation_owner("kai", "deploy") == key
        assert await host.relay.get_tty_reservation_owner("kai", "tty1") is None

    async def test_store_failure_rolls_the_relay_back(self, tmp_path: Path) -> None:
        relay = LocalRelay(tmp_path)

        async def _boom(_name: str) -> None:
            msg = "store write failed"
            raise OSError(msg)

        ctx = CliContext(
            relay=relay,
            config=BiffConfig(user="kai", repo_name="test"),
            session_key="kai:abc12345",
            user="kai",
            tty="abc12345",
            tty_name="old",
            rename_record=_boom,
        )
        await relay.reserve_tty_name("kai", "old", "kai:abc12345")
        await relay.update_session(
            UserSession(user="kai", tty="abc12345", tty_name="old")
        )

        with pytest.raises(OSError, match="store write failed"):
            await tty(ctx, "new")

        assert ctx.tty_name == "old"
        assert await relay.get_tty_reservation_owner("kai", "old") == "kai:abc12345"
        assert await relay.get_tty_reservation_owner("kai", "new") is None
        row = await relay.get_session("kai:abc12345")
        assert row is not None
        assert row.tty_name == "old"

    async def test_harness_client_has_no_record_to_update(
        self, ctx: CliContext
    ) -> None:
        assert ctx.rename_record is None
        assert not (await tty(ctx, "deploy")).error
