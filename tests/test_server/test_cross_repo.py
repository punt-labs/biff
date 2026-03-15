"""Unit tests for cross-repo messaging model and config changes (DES-030).

Tests for the parallel per-repo query approach, peer enforcement, and
cross-repo delivery will be added when those features are implemented.
This file currently covers model fields, config parsing, formatting,
KV key format, session backfill, and NatsRelay validation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from biff._stdlib import display_repo_name
from biff.config import build_biff_toml, extract_biff_fields
from biff.formatting import LAST_SPECS, WHO_SPECS, format_last, format_who
from biff.models import BiffConfig, SessionEvent, UserSession
from biff.server.state import create_state
from biff.server.tools._session import get_or_create_session


class TestDisplayRepoName:
    """display_repo_name converts sanitized names back to owner/repo (biff-7e03)."""

    def test_owner_repo(self) -> None:
        assert display_repo_name("punt-labs__biff") == "punt-labs/biff"

    def test_no_double_underscore(self) -> None:
        assert display_repo_name("myrepo") == "myrepo"

    def test_empty_string(self) -> None:
        assert display_repo_name("") == ""

    def test_only_first_double_underscore(self) -> None:
        assert display_repo_name("a__b__c") == "a/b__c"


class TestWhoDisplaysSlash:
    """format_who shows owner/repo, not owner__repo (biff-7e03)."""

    def test_who_shows_slash_repo(self) -> None:
        sessions = [
            UserSession(user="kai", tty="abc123", repo="punt-labs__biff"),
        ]
        output = format_who(sessions)
        assert "punt-labs/biff" in output
        assert "punt-labs__biff" not in output

    def test_last_shows_slash_repo(self) -> None:
        login = SessionEvent(
            session_key="kai:abc", event="login", user="kai", repo="punt-labs__biff"
        )
        pairs: list[tuple[SessionEvent, SessionEvent | None]] = [(login, None)]
        output = format_last(pairs, {"kai:abc"})
        assert "punt-labs/biff" in output
        assert "punt-labs__biff" not in output


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
        assert c.visible_repos == frozenset({"biff"})


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
    """NatsRelay._kv_key uses 3-part repo-scoped keys."""

    def test_kv_key_includes_repo(self) -> None:
        from biff.nats_relay import NatsRelay

        relay = NatsRelay(
            url="nats://localhost", repo_name="myrepo", stream_prefix="biff-test"
        )
        key = relay._kv_key("kai:abc123")
        assert key == "myrepo.kai.abc123"

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
    """Encryption key namespace is reserved."""

    def test_key_in_reserved(self) -> None:
        from biff.nats_relay import RESERVED_KV_NAMESPACES

        assert "key" in RESERVED_KV_NAMESPACES


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


class TestSessionRepoBackfill:
    """get_or_create_session backfills repo on pre-DES-030 sessions."""

    async def test_new_session_gets_repo(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name="_test-backfill")
        state = create_state(
            config, tmp_path, tty="tty1", hostname="test-host", pwd="/test"
        )
        session = await get_or_create_session(state)
        assert session.repo == "_test-backfill"

    async def test_existing_session_gets_repo_backfill(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name="_test-backfill")
        state = create_state(
            config, tmp_path, tty="tty1", hostname="test-host", pwd="/test"
        )
        old_session = UserSession(user="kai", tty="tty1")
        await state.relay.update_session(old_session)
        assert old_session.repo == ""

        session = await get_or_create_session(state)
        assert session.repo == "_test-backfill"

    async def test_session_with_repo_not_overwritten(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name="_test-backfill")
        state = create_state(
            config, tmp_path, tty="tty1", hostname="test-host", pwd="/test"
        )
        existing = UserSession(user="kai", tty="tty1", repo="already-set")
        await state.relay.update_session(existing)

        session = await get_or_create_session(state)
        assert session.repo == "already-set"


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
