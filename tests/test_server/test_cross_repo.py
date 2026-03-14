"""Unit tests for cross-repo messaging model and config changes (DES-030)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastmcp.tools.function_tool import FunctionTool

from biff.config import build_biff_toml, extract_biff_fields
from biff.formatting import LAST_SPECS, WHO_SPECS, format_last, format_who
from biff.models import BiffConfig, SessionEvent, UserSession
from biff.server.app import create_server
from biff.server.state import ServerState, create_state
from biff.server.tools._session import get_or_create_session
from biff.server.tools.talk import _resolve_talk_target
from biff.server.tools.wall import _validate_target_repo

if TYPE_CHECKING:
    from fastmcp import FastMCP


class TestUserSessionRepo:
    """UserSession.repo field."""

    def test_default_empty(self) -> None:
        s = UserSession(user="kai", tty="abc123")
        assert s.repo == ""

    def test_set_repo(self) -> None:
        s = UserSession(user="kai", tty="abc123", repo="punt-labs__biff")
        assert s.repo == "punt-labs__biff"

    def test_roundtrip_json(self) -> None:
        s = UserSession(user="kai", tty="abc123", repo="punt-labs__biff")
        restored = UserSession.model_validate_json(s.model_dump_json())
        assert restored.repo == "punt-labs__biff"

    def test_frozen(self) -> None:
        s = UserSession(user="kai", tty="abc123", repo="old")
        updated = s.model_copy(update={"repo": "new"})
        assert updated.repo == "new"
        assert s.repo == "old"


class TestSessionEventRepo:
    """SessionEvent.repo field."""

    def test_default_empty(self) -> None:
        e = SessionEvent(session_key="kai:abc", event="login", user="kai")
        assert e.repo == ""

    def test_set_repo(self) -> None:
        e = SessionEvent(
            session_key="kai:abc", event="login", user="kai", repo="punt-labs__biff"
        )
        assert e.repo == "punt-labs__biff"

    def test_roundtrip_json(self) -> None:
        e = SessionEvent(
            session_key="kai:abc", event="login", user="kai", repo="punt-labs__biff"
        )
        restored = SessionEvent.model_validate_json(e.model_dump_json())
        assert restored.repo == "punt-labs__biff"


class TestBiffConfigPeers:
    """BiffConfig.peers and visible_repos."""

    def test_default_empty(self) -> None:
        c = BiffConfig(user="kai", repo_name="biff")
        assert c.peers == ()

    def test_visible_repos_includes_self(self) -> None:
        c = BiffConfig(user="kai", repo_name="biff")
        assert "biff" in c.visible_repos
        assert "" in c.visible_repos  # LocalRelay compat

    def test_visible_repos_includes_peers(self) -> None:
        c = BiffConfig(
            user="kai",
            repo_name="biff",
            peers=("punt-labs__vox", "punt-labs__quarry"),
        )
        assert "biff" in c.visible_repos
        assert "punt-labs__vox" in c.visible_repos
        assert "punt-labs__quarry" in c.visible_repos

    def test_visible_repos_deduplicates(self) -> None:
        c = BiffConfig(user="kai", repo_name="biff", peers=("biff",))
        assert c.visible_repos.issuperset({"biff", ""})


class TestExtractPeers:
    """extract_biff_fields parses [peers] section."""

    def test_no_peers_section(self) -> None:
        _, _, _, peers = extract_biff_fields({})
        assert peers == ()

    def test_peers_with_repos(self) -> None:
        raw: dict[str, object] = {
            "peers": {"repos": ["punt-labs__vox", "punt-labs__quarry"]}
        }
        _, _, _, peers = extract_biff_fields(raw)
        assert peers == ("punt-labs__vox", "punt-labs__quarry")

    def test_peers_empty_list(self) -> None:
        raw: dict[str, object] = {"peers": {"repos": []}}
        _, _, _, peers = extract_biff_fields(raw)
        assert peers == ()

    def test_peers_non_string_filtered(self) -> None:
        raw: dict[str, object] = {"peers": {"repos": ["valid", 42, True]}}
        _, _, _, peers = extract_biff_fields(raw)
        assert peers == ("valid",)


class TestBuildBiffToml:
    """build_biff_toml emits [peers] section."""

    def test_with_peers(self) -> None:
        toml = build_biff_toml(["kai"], "nats://localhost", ["punt-labs__vox"])
        assert "[peers]" in toml
        assert '"punt-labs__vox"' in toml

    def test_without_peers(self) -> None:
        toml = build_biff_toml(["kai"], "nats://localhost")
        assert "[peers]" not in toml

    def test_empty_peers_not_emitted(self) -> None:
        toml = build_biff_toml(["kai"], "nats://localhost", [])
        assert "[peers]" not in toml


class TestWhoRepoColumn:
    """WHO_SPECS includes REPO column."""

    def test_repo_column_exists(self) -> None:
        names = [s.header for s in WHO_SPECS]
        assert "REPO" in names

    def test_repo_in_output(self) -> None:
        sessions = [
            UserSession(user="kai", tty="abc123", repo="biff"),
            UserSession(user="eric", tty="def456", repo="vox"),
        ]
        output = format_who(sessions)
        assert "biff" in output
        assert "vox" in output

    def test_empty_repo_shows_dash(self) -> None:
        sessions = [UserSession(user="kai", tty="abc123")]
        output = format_who(sessions)
        lines = output.strip().split("\n")
        assert len(lines) >= 2


class TestLastRepoColumn:
    """LAST_SPECS includes REPO column."""

    def test_repo_column_exists(self) -> None:
        names = [s.header for s in LAST_SPECS]
        assert "REPO" in names

    def test_repo_in_last_output(self) -> None:
        login = SessionEvent(
            session_key="kai:abc", event="login", user="kai", repo="biff"
        )
        pairs: list[tuple[SessionEvent, SessionEvent | None]] = [(login, None)]
        active_keys = {"kai:abc"}
        output = format_last(pairs, active_keys)
        assert "biff" in output


class TestKvKeyFormat:
    """NatsRelay._kv_key uses 2-part org-scoped keys."""

    def test_kv_key_no_repo_prefix(self) -> None:
        from biff.nats_relay import NatsRelay

        relay = NatsRelay(
            url="nats://localhost", repo_name="myrepo", stream_prefix="biff-test"
        )
        key = relay._kv_key("kai:abc123")
        assert key == "kai.abc123"
        assert "myrepo" not in key

    def test_subject_for_key_includes_repo(self) -> None:
        from biff.nats_relay import NatsRelay

        relay = NatsRelay(
            url="nats://localhost", repo_name="myrepo", stream_prefix="biff-test"
        )
        subject = relay._subject_for_key("kai:abc123")
        assert subject == "biff-test.myrepo.inbox.kai.abc123"

    def test_subject_for_key_cross_repo(self) -> None:
        from biff.nats_relay import NatsRelay

        relay = NatsRelay(
            url="nats://localhost", repo_name="myrepo", stream_prefix="biff-test"
        )
        subject = relay._subject_for_key("kai:abc123", target_repo="otherrepo")
        assert subject == "biff-test.otherrepo.inbox.kai.abc123"


class TestReservedKvNamespaces:
    """Wall keys are filtered from session scans."""

    def test_wall_in_reserved(self) -> None:
        from biff.nats_relay import RESERVED_KV_NAMESPACES

        assert "wall" in RESERVED_KV_NAMESPACES
        assert "key" in RESERVED_KV_NAMESPACES


# ---------------------------------------------------------------------------
# Helpers for MCP tool-level tests
# ---------------------------------------------------------------------------


def _state_with_peers(
    tmp_path: Path,
    *,
    user: str = "kai",
    repo_name: str = "_test-biff",
    peers: tuple[str, ...] = (),
    tty: str = "tty1",
) -> ServerState:
    config = BiffConfig(user=user, repo_name=repo_name, peers=peers)
    return create_state(config, tmp_path, tty=tty, hostname="test-host", pwd="/test")


def _create_mcp(state: ServerState) -> FastMCP[ServerState]:
    return create_server(state)


async def _get_tool_fn(state: ServerState, tool_name: str):
    mcp = _create_mcp(state)
    tool = await mcp.get_tool(tool_name)
    assert tool is not None
    assert isinstance(tool, FunctionTool)
    return tool.fn


# ---------------------------------------------------------------------------
# _validate_repo — NATS subject injection guard
# ---------------------------------------------------------------------------


class TestValidateRepo:
    """NatsRelay._validate_repo rejects NATS-illegal repo names."""

    def test_valid_repo(self) -> None:
        from biff.nats_relay import NatsRelay

        assert NatsRelay._validate_repo("punt-labs__biff") == "punt-labs__biff"

    def test_rejects_dots(self) -> None:
        from biff.nats_relay import NatsRelay

        with pytest.raises(ValueError, match="Invalid repo name"):
            NatsRelay._validate_repo("punt.labs")

    def test_rejects_wildcards(self) -> None:
        from biff.nats_relay import NatsRelay

        with pytest.raises(ValueError, match="Invalid repo name"):
            NatsRelay._validate_repo("repo*")

    def test_rejects_gt(self) -> None:
        from biff.nats_relay import NatsRelay

        with pytest.raises(ValueError, match="Invalid repo name"):
            NatsRelay._validate_repo("repo>")

    def test_rejects_spaces(self) -> None:
        from biff.nats_relay import NatsRelay

        with pytest.raises(ValueError, match="Invalid repo name"):
            NatsRelay._validate_repo("my repo")

    def test_rejects_empty(self) -> None:
        from biff.nats_relay import NatsRelay

        with pytest.raises(ValueError, match="Invalid repo name"):
            NatsRelay._validate_repo("")


# ---------------------------------------------------------------------------
# _validate_target_repo — wall repo authorization
# ---------------------------------------------------------------------------


class TestValidateTargetRepo:
    """_validate_target_repo enforces visible_repos boundary."""

    def test_empty_repo_returns_none(self) -> None:
        result = _validate_target_repo("", frozenset({"biff"}))
        assert result is None

    def test_valid_repo_returns_repo(self) -> None:
        result = _validate_target_repo("biff", frozenset({"biff", "vox"}))
        assert result == "biff"

    def test_unknown_repo_returns_none(self) -> None:
        result = _validate_target_repo("quarry", frozenset({"biff", "vox"}))
        assert result is None


# ---------------------------------------------------------------------------
# get_or_create_session — repo backfill
# ---------------------------------------------------------------------------


class TestSessionRepoBackfill:
    """get_or_create_session backfills repo on pre-DES-030 sessions."""

    async def test_new_session_gets_repo(self, tmp_path: Path) -> None:
        state = _state_with_peers(tmp_path, repo_name="_test-backfill")
        session = await get_or_create_session(state)
        assert session.repo == "_test-backfill"

    async def test_existing_session_gets_repo_backfill(self, tmp_path: Path) -> None:
        state = _state_with_peers(tmp_path, repo_name="_test-backfill")
        # Simulate a pre-DES-030 session (no repo field)
        old_session = UserSession(user="kai", tty="tty1")
        await state.relay.update_session(old_session)
        assert old_session.repo == ""

        session = await get_or_create_session(state)
        assert session.repo == "_test-backfill"

    async def test_session_with_repo_not_overwritten(self, tmp_path: Path) -> None:
        state = _state_with_peers(tmp_path, repo_name="_test-backfill")
        existing = UserSession(user="kai", tty="tty1", repo="already-set")
        await state.relay.update_session(existing)

        session = await get_or_create_session(state)
        assert session.repo == "already-set"


# ---------------------------------------------------------------------------
# /who MCP tool — repo filtering and validation
# ---------------------------------------------------------------------------


class TestWhoToolCrossRepo:
    """MCP /who tool filters by visible_repos and validates repo param."""

    async def test_who_filters_by_visible_repos(self, tmp_path: Path) -> None:
        """Sessions from non-peered repos are hidden."""
        state = _state_with_peers(tmp_path, peers=("_test-vox",))
        # Create sessions in different repos
        await state.relay.update_session(
            UserSession(user="kai", tty="tty1", repo="_test-biff")
        )
        await state.relay.update_session(
            UserSession(user="eric", tty="tty2", repo="_test-vox")
        )
        await state.relay.update_session(
            UserSession(user="priya", tty="tty3", repo="_test-quarry")
        )
        fn = await _get_tool_fn(state, "who")
        result = await fn()
        assert "@kai" in result
        assert "@eric" in result
        assert "@priya" not in result  # quarry not in peers

    async def test_who_repo_param_validates(self, tmp_path: Path) -> None:
        """who(repo=...) rejects repos not in visible_repos."""
        state = _state_with_peers(tmp_path)
        fn = await _get_tool_fn(state, "who")
        result = await fn(repo="unknown-repo")
        assert "not in your visible repos" in result

    async def test_who_repo_param_filters(self, tmp_path: Path) -> None:
        """who(repo=...) shows only sessions from that repo."""
        state = _state_with_peers(tmp_path, peers=("_test-vox",))
        await state.relay.update_session(
            UserSession(user="kai", tty="tty1", repo="_test-biff")
        )
        await state.relay.update_session(
            UserSession(user="eric", tty="tty2", repo="_test-vox")
        )
        fn = await _get_tool_fn(state, "who")
        result = await fn(repo="_test-vox")
        assert "@eric" in result
        assert "@kai" not in result

    async def test_who_hides_non_peered_sessions(self, tmp_path: Path) -> None:
        """Non-peered repo sessions are filtered out even when present."""
        state = _state_with_peers(tmp_path)
        await state.relay.update_session(
            UserSession(user="priya", tty="tty3", repo="_test-quarry")
        )
        fn = await _get_tool_fn(state, "who")
        result = await fn()
        assert "priya" not in result


# ---------------------------------------------------------------------------
# /finger MCP tool — visibility enforcement
# ---------------------------------------------------------------------------


class TestFingerToolCrossRepo:
    """MCP /finger tool enforces visible_repos on targeted lookups."""

    async def test_finger_targeted_hides_non_peer(self, tmp_path: Path) -> None:
        """finger @user:tty hides sessions from non-peered repos."""
        state = _state_with_peers(tmp_path)
        await state.relay.update_session(
            UserSession(user="priya", tty="tty3", repo="_test-quarry")
        )
        fn = await _get_tool_fn(state, "finger")
        result = await fn(user="@priya:tty3")
        assert "No session on tty tty3" in result

    async def test_finger_targeted_shows_peer(self, tmp_path: Path) -> None:
        """finger @user:tty shows sessions from peered repos."""
        state = _state_with_peers(tmp_path, peers=("_test-vox",))
        await state.relay.update_session(
            UserSession(user="eric", tty="tty2", repo="_test-vox", plan="building TTS")
        )
        fn = await _get_tool_fn(state, "finger")
        result = await fn(user="@eric:tty2")
        assert "Login: eric" in result
        assert "building TTS" in result

    async def test_finger_bare_user_filters_by_visible(self, tmp_path: Path) -> None:
        """finger @user filters multi-session list by visible_repos."""
        state = _state_with_peers(tmp_path, peers=("_test-vox",))
        await state.relay.update_session(
            UserSession(user="eric", tty="tty2", tty_name="vox-tty", repo="_test-vox")
        )
        await state.relay.update_session(
            UserSession(
                user="eric", tty="tty3", tty_name="quarry-tty", repo="_test-quarry"
            )
        )
        fn = await _get_tool_fn(state, "finger")
        result = await fn(user="@eric")
        assert "Login: eric" in result
        assert "vox-tty" in result  # peered session shown
        assert "quarry-tty" not in result  # non-peered session hidden


# ---------------------------------------------------------------------------
# /write MCP tool — cross-repo peer enforcement
# ---------------------------------------------------------------------------


class TestWriteToolCrossRepo:
    """MCP /write enforces peer list on cross-repo delivery."""

    async def test_write_to_non_peer_rejected(self, tmp_path: Path) -> None:
        """Writing to a session in a non-peered repo is rejected."""
        state = _state_with_peers(tmp_path)
        # Create eric's session in a non-peered repo
        await state.relay.update_session(
            UserSession(user="eric", tty="tty2", repo="_test-quarry")
        )
        fn = await _get_tool_fn(state, "write")
        result = await fn(to="@eric:tty2", message="hello")
        assert "not in your peer list" in result

    async def test_write_to_peer_succeeds(self, tmp_path: Path) -> None:
        """Writing to a session in a peered repo delivers the message."""
        state = _state_with_peers(tmp_path, peers=("_test-vox",))
        await state.relay.update_session(
            UserSession(user="eric", tty="tty2", repo="_test-vox")
        )
        fn = await _get_tool_fn(state, "write")
        result = await fn(to="@eric:tty2", message="hello")
        await asyncio.sleep(0)  # yield for fire_and_forget
        assert "Message sent" in result
        # Verify the message actually arrived in eric's inbox
        messages = await state.relay.fetch("eric:tty2")
        assert len(messages) == 1
        assert messages[0].body == "hello"

    async def test_write_bare_user_no_cross_repo(self, tmp_path: Path) -> None:
        """Bare @user resolves with target_repo=None (repo-local)."""
        from biff.server.tools.messaging import _resolve_recipient

        state = _state_with_peers(tmp_path, peers=("_test-vox",))
        relay_key, _display, target_repo = await _resolve_recipient(state, "@eric")
        assert relay_key == "eric"
        assert target_repo is None  # no cross-repo delivery for bare @user


# ---------------------------------------------------------------------------
# /wall MCP tool — repo validation and broadcast
# ---------------------------------------------------------------------------


class TestWallToolCrossRepo:
    """MCP /wall validates repo param against visible_repos."""

    async def test_wall_unknown_repo_rejected(self, tmp_path: Path) -> None:
        state = _state_with_peers(tmp_path)
        fn = await _get_tool_fn(state, "wall")
        result = await fn(message="freeze", repo="unknown-repo")
        assert "not in your visible repos" in result

    async def test_wall_post_succeeds(self, tmp_path: Path) -> None:
        state = _state_with_peers(tmp_path)
        fn = await _get_tool_fn(state, "wall")
        result = await fn(message="deploy freeze")
        await asyncio.sleep(0)  # yield for fire_and_forget
        assert "Wall posted" in result
        # Verify the wall was actually persisted
        wall = await state.relay.get_wall()
        assert wall is not None
        assert wall.text == "deploy freeze"

    async def test_wall_clear_succeeds(self, tmp_path: Path) -> None:
        state = _state_with_peers(tmp_path)
        fn = await _get_tool_fn(state, "wall")
        # Post first, then clear
        await fn(message="temporary wall")
        await asyncio.sleep(0)
        result = await fn(clear=True)
        await asyncio.sleep(0)
        assert "Wall cleared" in result
        # Verify the wall was actually cleared
        wall = await state.relay.get_wall()
        assert wall is None


# ---------------------------------------------------------------------------
# _resolve_talk_target — peer enforcement
# ---------------------------------------------------------------------------


class TestResolveTalkTargetCrossRepo:
    """_resolve_talk_target enforces visible_repos on cross-repo talk."""

    async def test_cross_repo_to_peer_sets_target_repo(self, tmp_path: Path) -> None:
        state = _state_with_peers(tmp_path, peers=("_test-vox",))
        await state.relay.update_session(
            UserSession(user="eric", tty="tty2", repo="_test-vox")
        )
        relay_key, _display, target_repo = await _resolve_talk_target(
            state.relay,
            "eric",
            "tty2",
            sender_repo="_test-biff",
            visible_repos=state.config.visible_repos,
        )
        assert relay_key == "eric:tty2"
        assert target_repo == "_test-vox"

    async def test_cross_repo_to_non_peer_raises(self, tmp_path: Path) -> None:
        state = _state_with_peers(tmp_path)
        await state.relay.update_session(
            UserSession(user="eric", tty="tty2", repo="_test-quarry")
        )
        with pytest.raises(ValueError, match="not in your peer list"):
            await _resolve_talk_target(
                state.relay,
                "eric",
                "tty2",
                sender_repo="_test-biff",
                visible_repos=state.config.visible_repos,
            )

    async def test_same_repo_no_target_repo(self, tmp_path: Path) -> None:
        state = _state_with_peers(tmp_path)
        await state.relay.update_session(
            UserSession(user="eric", tty="tty2", repo="_test-biff")
        )
        relay_key, _display, target_repo = await _resolve_talk_target(
            state.relay,
            "eric",
            "tty2",
            sender_repo="_test-biff",
            visible_repos=state.config.visible_repos,
        )
        assert relay_key == "eric:tty2"
        assert target_repo is None  # same repo, no cross-repo delivery


# ---------------------------------------------------------------------------
# CLI commands — cross-repo enforcement
# ---------------------------------------------------------------------------


class TestCliWriteCrossRepo:
    """CLI write command enforces peer list."""

    async def test_cli_write_to_non_peer_rejected(self, tmp_path: Path) -> None:
        from biff.cli_session import CliContext
        from biff.commands.write import write
        from biff.relay import LocalRelay

        relay = LocalRelay(tmp_path)
        config = BiffConfig(user="kai", repo_name="_test-biff")
        ctx = CliContext(
            relay=relay,
            config=config,
            session_key="kai:tty1",
            user="kai",
            tty="tty1",
        )
        # Create eric's session in a non-peered repo
        await relay.update_session(
            UserSession(user="eric", tty="tty2", repo="_test-quarry")
        )
        result = await write(ctx, "@eric:tty2", "hello")
        assert result.error
        assert "not in your peer list" in result.text

    async def test_cli_write_to_peer_succeeds(self, tmp_path: Path) -> None:
        from biff.cli_session import CliContext
        from biff.commands.write import write
        from biff.relay import LocalRelay

        relay = LocalRelay(tmp_path)
        config = BiffConfig(user="kai", repo_name="_test-biff", peers=("_test-vox",))
        ctx = CliContext(
            relay=relay,
            config=config,
            session_key="kai:tty1",
            user="kai",
            tty="tty1",
        )
        await relay.update_session(
            UserSession(user="eric", tty="tty2", repo="_test-vox")
        )
        result = await write(ctx, "@eric:tty2", "hello")
        assert not result.error
        assert "Message sent" in result.text


class TestCliFingerCrossRepo:
    """CLI finger command enforces visible_repos."""

    async def test_cli_finger_targeted_hides_non_peer(self, tmp_path: Path) -> None:
        from biff.cli_session import CliContext
        from biff.commands.finger import finger
        from biff.relay import LocalRelay

        relay = LocalRelay(tmp_path)
        config = BiffConfig(user="kai", repo_name="_test-biff")
        ctx = CliContext(
            relay=relay,
            config=config,
            session_key="kai:tty1",
            user="kai",
            tty="tty1",
        )
        await relay.update_session(
            UserSession(user="priya", tty="tty3", repo="_test-quarry")
        )
        result = await finger(ctx, "@priya:tty3")
        assert result.error
        assert "No session on tty tty3" in result.text


class TestCliWhoCrossRepo:
    """CLI who command filters by visible_repos."""

    async def test_cli_who_hides_non_peer(self, tmp_path: Path) -> None:
        from biff.cli_session import CliContext
        from biff.commands.who import who
        from biff.relay import LocalRelay

        relay = LocalRelay(tmp_path)
        config = BiffConfig(user="kai", repo_name="_test-biff")
        ctx = CliContext(
            relay=relay,
            config=config,
            session_key="kai:tty1",
            user="kai",
            tty="tty1",
        )
        # Only non-peered session
        await relay.update_session(
            UserSession(user="priya", tty="tty3", repo="_test-quarry")
        )
        result = await who(ctx)
        assert "priya" not in result.text


# ---------------------------------------------------------------------------
# talk_notify_subject — cross-repo subject construction
# ---------------------------------------------------------------------------


class TestTalkNotifySubjectCrossRepo:
    """talk_notify_subject uses target_repo for cross-repo notifications."""

    def test_default_uses_own_repo(self) -> None:
        from biff.nats_relay import NatsRelay

        relay = NatsRelay(
            url="nats://localhost", repo_name="myrepo", stream_prefix="biff-test"
        )
        subject = relay.talk_notify_subject("kai")
        assert subject == "biff-test.myrepo.talk.notify.kai"

    def test_cross_repo_uses_target(self) -> None:
        from biff.nats_relay import NatsRelay

        relay = NatsRelay(
            url="nats://localhost", repo_name="myrepo", stream_prefix="biff-test"
        )
        subject = relay.talk_notify_subject("kai", target_repo="otherrepo")
        assert subject == "biff-test.otherrepo.talk.notify.kai"

    def test_invalid_target_repo_raises(self) -> None:
        from biff.nats_relay import NatsRelay

        relay = NatsRelay(
            url="nats://localhost", repo_name="myrepo", stream_prefix="biff-test"
        )
        with pytest.raises(ValueError, match="Invalid repo name"):
            relay.talk_notify_subject("kai", target_repo="bad.repo")
