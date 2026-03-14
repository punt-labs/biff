"""Unit tests for cross-repo messaging model and config changes (DES-030)."""

from __future__ import annotations

from biff.config import build_biff_toml, extract_biff_fields
from biff.formatting import LAST_SPECS, WHO_SPECS, format_last, format_who
from biff.models import BiffConfig, SessionEvent, UserSession


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
        assert c.visible_repos == frozenset({"biff"})

    def test_visible_repos_includes_peers(self) -> None:
        c = BiffConfig(
            user="kai",
            repo_name="biff",
            peers=("punt-labs__vox", "punt-labs__quarry"),
        )
        assert c.visible_repos == frozenset(
            {"biff", "punt-labs__vox", "punt-labs__quarry"}
        )

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
