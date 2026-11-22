"""Tests for ``biff.commands.write``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from biff.cli_context import CliContext
from biff.commands._messaging import Delivery
from biff.commands.write import write
from biff.models import BiffConfig, SessionEvent, UserSession
from biff.relay import LocalRelay
from biff.server.tools._write_target import WriteRouter

from ._relay import WtmpRelay


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
        target = WriteRouter(local_repo="", visible_repos=set()).resolve(
            [UserSession(user="eric", tty="aa", tty_name="tty1")],
            "eric",
            "tty9",
        )
        failed = Delivery(text="Could not deliver to eric", ok=False, parts=1)
        assert target.report(failed) == "Could not deliver to eric"


class TestResolveWriteTarget:
    def test_bare_user_routes_to_recipients_repo(self) -> None:
        # A bare write from repo-a must reach the mailbox of repo-b, where
        # the recipient reads; repo-a's mailbox is one they never drain.
        sessions = [UserSession(user="eric", tty="aa", tty_name="tty1", repo="repo-b")]
        target = WriteRouter(
            local_repo="repo-a", visible_repos={"repo-a", "repo-b"}
        ).resolve(sessions, "eric", None)
        assert not target.forwarded
        assert target.relay_key == "eric"
        assert target.target_repo == "repo-b"

    def test_bare_user_stays_local_when_recipient_is_local(self) -> None:
        sessions = [
            UserSession(user="eric", tty="aa", tty_name="tty1", repo="repo-a"),
            UserSession(user="eric", tty="bb", tty_name="tty2", repo="repo-b"),
        ]
        target = WriteRouter(
            local_repo="repo-a", visible_repos={"repo-a", "repo-b"}
        ).resolve(sessions, "eric", None)
        assert target.target_repo is None

    def test_bare_unknown_user_stays_local(self) -> None:
        # Offline mail to a user with no session anywhere keeps the local
        # mailbox, as before.
        target = WriteRouter(
            local_repo="repo-a", visible_repos={"repo-a", "repo-b"}
        ).resolve([], "eric", None)
        assert target.target_repo is None
        assert target.relay_key == "eric"

    def test_no_live_session_picks_most_recent_repo(self) -> None:
        # A CLI user is rarely live (each command attaches and exits), so
        # the repo they used last is the one they will read next.
        now = datetime.now(UTC)
        sessions = [
            UserSession(
                user="eric",
                tty="aa",
                tty_name="tty1",
                repo="repo-b",
                last_active=now - timedelta(hours=5),
            ),
            UserSession(
                user="eric",
                tty="bb",
                tty_name="tty2",
                repo="repo-c",
                last_active=now - timedelta(minutes=10),
            ),
        ]
        router = WriteRouter(
            local_repo="repo-a", visible_repos={"repo-a", "repo-b", "repo-c"}
        )
        assert router.resolve(sessions, "eric", None).target_repo == "repo-c"
        assert router.resolve(sessions, "eric", "tty9").target_repo == "repo-c"

    def test_forward_routes_to_recipients_repo(self) -> None:
        # The recipient restarted into repo-b; a mailbox forward sent from
        # repo-a must be published to repo-b, where they poll.
        sessions = [UserSession(user="eric", tty="aa", tty_name="tty1", repo="repo-b")]
        target = WriteRouter(
            local_repo="repo-a", visible_repos={"repo-a", "repo-b"}
        ).resolve(
            sessions,
            "eric",
            "tty9",
        )
        assert target.forwarded
        assert target.target_repo == "repo-b"

    def test_forward_stays_local_when_recipient_is_local(self) -> None:
        sessions = [
            UserSession(user="eric", tty="aa", tty_name="tty1", repo="repo-a"),
            UserSession(user="eric", tty="bb", tty_name="tty2", repo="repo-b"),
        ]
        target = WriteRouter(
            local_repo="repo-a", visible_repos={"repo-a", "repo-b"}
        ).resolve(
            sessions,
            "eric",
            "tty9",
        )
        assert target.target_repo is None

    def test_forward_picks_first_repo_by_name_when_not_local(self) -> None:
        sessions = [
            UserSession(user="eric", tty="aa", tty_name="tty1", repo="repo-c"),
            UserSession(user="eric", tty="bb", tty_name="tty2", repo="repo-b"),
        ]
        target = WriteRouter(
            local_repo="repo-a", visible_repos={"repo-a", "repo-b", "repo-c"}
        ).resolve(
            sessions,
            "eric",
            "tty9",
        )
        assert target.target_repo == "repo-b"

    def test_forward_prefers_live_repo_over_stale_local_row(self) -> None:
        # A stale row in the sender's repo must not win: the recipient only
        # drains the mailbox of the repo where they are live.
        stale = datetime.now(UTC) - timedelta(hours=1)
        sessions = [
            UserSession(
                user="eric",
                tty="aa",
                tty_name="tty1",
                repo="repo-a",
                last_active=stale,
            ),
            UserSession(user="eric", tty="bb", tty_name="tty2", repo="repo-b"),
        ]
        target = WriteRouter(
            local_repo="repo-a", visible_repos={"repo-a", "repo-b"}
        ).resolve(
            sessions,
            "eric",
            "tty9",
        )
        assert target.target_repo == "repo-b"

    def test_forward_with_no_live_session_still_picks_a_repo(self) -> None:
        stale = datetime.now(UTC) - timedelta(hours=1)
        sessions = [
            UserSession(
                user="eric",
                tty="aa",
                tty_name="tty1",
                repo="repo-c",
                last_active=stale,
            ),
            UserSession(
                user="eric",
                tty="bb",
                tty_name="tty2",
                repo="repo-b",
                last_active=stale,
            ),
        ]
        target = WriteRouter(
            local_repo="repo-a", visible_repos={"repo-a", "repo-b", "repo-c"}
        ).resolve(
            sessions,
            "eric",
            "tty9",
        )
        assert target.target_repo == "repo-b"

    def test_forward_goes_to_addressed_repo_over_live_repo(self) -> None:
        # The named session ran in repo-b and ended; the user is still live
        # in repo-a.  The forward must reach repo-b, the repo addressed.
        sessions = [UserSession(user="eric", tty="aa", tty_name="tty1", repo="repo-a")]
        target = WriteRouter(
            local_repo="repo-c", visible_repos={"repo-a", "repo-b", "repo-c"}
        ).resolve(
            sessions,
            "eric",
            "qa",
            addressed_repo="repo-b",
        )
        assert target.forwarded
        assert target.target_repo == "repo-b"
        assert "eric's mailbox in repo-b" in target.notice

    def test_forward_to_addressed_repo_needs_no_session_row(self) -> None:
        target = WriteRouter(
            local_repo="repo-a", visible_repos={"repo-a", "repo-b"}
        ).resolve(
            [],
            "eric",
            "qa",
            addressed_repo="repo-a",
        )
        assert target.forwarded
        assert target.target_repo is None

    def test_invisible_addressed_repo_falls_back_to_present_repo(self) -> None:
        sessions = [UserSession(user="eric", tty="aa", tty_name="tty1", repo="repo-b")]
        target = WriteRouter(
            local_repo="repo-a", visible_repos={"repo-a", "repo-b"}
        ).resolve(
            sessions,
            "eric",
            "qa",
            addressed_repo="repo-secret",
        )
        assert target.target_repo == "repo-b"


class TestWriteToEndedNamedSession:
    """A write to an ended named session reaches the repo it ran in."""

    @staticmethod
    async def _ended_qa_session(relay: WtmpRelay) -> None:
        # eric is live in repo-a; his named session "qa" ran in repo-b
        # and has ended, leaving only its wtmp history.
        await relay.update_session(
            UserSession(user="eric", tty="aa", tty_name="tty1", repo="repo-a")
        )
        await relay.append_wtmp(
            SessionEvent(
                session_key="eric:bb",
                event="logout",
                user="eric",
                tty="bb",
                tty_name="qa",
                repo="repo-b",
            )
        )

    async def test_bare_write_finds_recipients_repo(self, tmp_path: Path) -> None:
        relay = WtmpRelay(tmp_path)
        await self._ended_qa_session(relay)
        target = await WriteRouter(
            local_repo="repo-c", visible_repos=frozenset({"repo-a", "repo-b", "repo-c"})
        ).find(relay, "eric", None)
        assert not target.forwarded
        assert target.target_repo == "repo-a"

    async def test_target_is_ended_sessions_repo(self, tmp_path: Path) -> None:
        relay = WtmpRelay(tmp_path)
        await self._ended_qa_session(relay)
        target = await WriteRouter(
            local_repo="repo-c", visible_repos=frozenset({"repo-a", "repo-b", "repo-c"})
        ).find(relay, "eric", "qa")
        assert target.forwarded
        assert target.relay_key == "eric"
        assert target.target_repo == "repo-b"

    async def test_write_reports_ended_sessions_repo(self, tmp_path: Path) -> None:
        relay = WtmpRelay(tmp_path)
        await self._ended_qa_session(relay)
        ctx = CliContext(
            relay=relay,
            config=BiffConfig(
                user="kai", repo_name="repo-c", peers=("repo-a", "repo-b")
            ),
            session_key="kai:abc12345",
            user="kai",
            tty="abc12345",
        )
        result = await write(ctx, "@eric:qa", "hello")
        assert not result.error
        assert "qa has no active session" in result.text
        assert "eric's mailbox in repo-b" in result.text
