"""Tests for ``biff.commands.write``."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands._messaging import Delivery
from biff.commands.write import write
from biff.models import UserSession
from biff.relay import LocalRelay
from biff.server.tools._session import resolve_write_target


class TestWrite:
    async def test_send_to_user(self, ctx: CliContext, relay: LocalRelay) -> None:
        await relay.update_session(
            UserSession(user="eric", tty="abc12345", tty_name="tty1")
        )
        result = await write(ctx, "@eric", "Hello!")
        assert not result.error
        assert "Message sent to eric" in result.text
        assert result.json_data == {"status": "sent", "to": "eric", "parts": 1}

        # Verify message was delivered
        msgs = await relay.fetch_user_inbox("eric")
        assert len(msgs) == 1
        assert msgs[0].body == "Hello!"
        assert msgs[0].from_user == "kai"

    async def test_long_message_chunked(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await relay.update_session(
            UserSession(user="eric", tty="abc12345", tty_name="tty1")
        )
        long_msg = " ".join(["word"] * 200)  # ~1000 chars
        result = await write(ctx, "@eric", long_msg)
        assert not result.error
        msgs = await relay.fetch_user_inbox("eric")
        assert len(msgs) == 2
        assert all(len(m.body) <= 512 for m in msgs)
        reconstructed = " ".join(m.body for m in msgs)
        assert reconstructed == long_msg
        assert "(2 parts)" in result.text
        json_data = result.json_data
        assert isinstance(json_data, dict)
        assert json_data["parts"] == 2

    async def test_short_message_no_parts_label(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await relay.update_session(
            UserSession(user="eric", tty="abc12345", tty_name="tty1")
        )
        result = await write(ctx, "@eric", "short")
        assert not result.error
        assert "parts" not in result.text
        msgs = await relay.fetch_user_inbox("eric")
        assert len(msgs) == 1
        assert msgs[0].body == "short"

    async def test_send_to_tty(self, ctx: CliContext, relay: LocalRelay) -> None:
        await relay.update_session(
            UserSession(user="eric", tty="abc12345", tty_name="tty1")
        )
        result = await write(ctx, "@eric:tty1", "targeted msg")
        assert not result.error
        assert "eric:tty1" in result.text

    async def test_targeted_nonexistent_tty_error(self, ctx: CliContext) -> None:
        result = await write(ctx, "@eric:tty99", "hello")
        assert result.error
        assert "not found" in result.text

    async def test_stale_tty_forwards_to_mailbox(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await relay.update_session(
            UserSession(user="eric", tty="abc12345", tty_name="tty1")
        )
        result = await write(ctx, "@eric:tty99", "hello")
        assert not result.error
        assert result.json_data == {
            "status": "forwarded",
            "to": "eric",
            "parts": 1,
        }
        assert "tty99 has no active session" in result.text
        assert "delivered to eric's mailbox" in result.text
        msgs = await relay.fetch_user_inbox("eric")
        assert [m.body for m in msgs] == ["hello"]

    async def test_live_holder_wins_over_mailbox(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await relay.update_session(
            UserSession(user="eric", tty="abc12345", tty_name="tty1")
        )
        result = await write(ctx, "@eric:tty1", "targeted")
        assert not result.error
        json_data = result.json_data
        assert isinstance(json_data, dict)
        assert json_data["status"] == "sent"
        assert await relay.fetch_user_inbox("eric") == []
        assert len(await relay.fetch("eric:abc12345")) == 1

    async def test_targeted_unknown_user_error(self, ctx: CliContext) -> None:
        result = await write(ctx, "@nobody:tty1", "hello")
        assert result.error
        assert "not found in visible repos" in result.text

    async def test_resolve_session_by_tty_name(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        # Register eric with a named TTY
        await relay.update_session(
            UserSession(user="eric", tty="def67890", tty_name="dev")
        )
        result = await write(ctx, "@eric:dev", "resolved!")
        assert not result.error
        assert "eric:dev" in result.text

    async def test_bare_user_without_at(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await relay.update_session(
            UserSession(user="eric", tty="abc12345", tty_name="tty1")
        )
        result = await write(ctx, "eric", "no prefix")
        assert not result.error
        msgs = await relay.fetch_user_inbox("eric")
        assert len(msgs) == 1
        assert msgs[0].body == "no prefix"

    async def test_bare_user_without_at_delivers(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        # Bare string without @ now delivers like @user (no validation gate)
        result = await write(ctx, "nobody", "hello")
        assert not result.error
        msgs = await relay.fetch_user_inbox("nobody")
        assert len(msgs) == 1
        assert msgs[0].body == "hello"

    async def test_broadcast_to_offline_user_succeeds(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        # @user with @ prefix allows offline delivery — no session needed
        result = await write(ctx, "@offlineuser", "offline msg")
        assert not result.error
        msgs = await relay.fetch_user_inbox("offlineuser")
        assert len(msgs) == 1
        assert msgs[0].body == "offline msg"

    async def test_bare_existing_user_succeeds(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await relay.update_session(
            UserSession(user="eric", tty="abc12345", tty_name="tty1")
        )
        result = await write(ctx, "@eric", "hello")
        assert not result.error
        msgs = await relay.fetch_user_inbox("eric")
        assert len(msgs) == 1

    async def test_invalid_address_empty_tty(self, ctx: CliContext) -> None:
        result = await write(ctx, "@eric:", "hello")
        assert result.error
        assert "Empty TTY" in result.text
        assert result.json_data == {
            "status": "error",
            "to": "@eric:",
            "error": result.text,
        }


class TestForwardReport:
    async def test_forwarded_multi_chunk_reports_parts(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        await relay.update_session(
            UserSession(user="eric", tty="abc12345", tty_name="tty1")
        )
        result = await write(ctx, "@eric:tty99", " ".join(["word"] * 200))
        assert not result.error
        assert "(2 parts)" in result.text
        assert "delivered to eric's mailbox" in result.text

    def test_failed_delivery_is_not_reported_as_forwarded(self) -> None:
        target = resolve_write_target(
            [UserSession(user="eric", tty="aa", tty_name="tty1")],
            "eric",
            "tty9",
            local_repo="",
            visible_repos=set(),
        )
        failed = Delivery(text="Could not deliver to eric", ok=False, parts=1)
        assert target.report(failed) == "Could not deliver to eric"


class TestResolveWriteTarget:
    def test_forward_routes_to_recipients_repo(self) -> None:
        # The recipient restarted into repo-b; a mailbox forward sent from
        # repo-a must be published to repo-b, where they poll.
        sessions = [UserSession(user="eric", tty="aa", tty_name="tty1", repo="repo-b")]
        target = resolve_write_target(
            sessions,
            "eric",
            "tty9",
            local_repo="repo-a",
            visible_repos={"repo-a", "repo-b"},
        )
        assert target.forwarded
        assert target.target_repo == "repo-b"

    def test_forward_stays_local_when_recipient_is_local(self) -> None:
        sessions = [
            UserSession(user="eric", tty="aa", tty_name="tty1", repo="repo-a"),
            UserSession(user="eric", tty="bb", tty_name="tty2", repo="repo-b"),
        ]
        target = resolve_write_target(
            sessions,
            "eric",
            "tty9",
            local_repo="repo-a",
            visible_repos={"repo-a", "repo-b"},
        )
        assert target.target_repo is None

    def test_forward_picks_first_repo_by_name_when_not_local(self) -> None:
        sessions = [
            UserSession(user="eric", tty="aa", tty_name="tty1", repo="repo-c"),
            UserSession(user="eric", tty="bb", tty_name="tty2", repo="repo-b"),
        ]
        target = resolve_write_target(
            sessions,
            "eric",
            "tty9",
            local_repo="repo-a",
            visible_repos={"repo-a", "repo-b", "repo-c"},
        )
        assert target.target_repo == "repo-b"
