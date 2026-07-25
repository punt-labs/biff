"""Pure NatsRelay key-construction tests (no server, default tier)."""

from __future__ import annotations

import pytest

from biff.nats_relay import NatsRelay


def _relay() -> NatsRelay:
    """A NatsRelay constructed for key-building only — never connected."""
    return NatsRelay(repo_name="_test-keys", stream_prefix="test")


class TestSidHintKey:
    """The session_id reclaim-hint key validates both segments (biff-7ak P2)."""

    def test_valid_session_id_builds_key(self) -> None:
        key = _relay()._sid_hint_key("kai", "2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b")
        assert key == "kai.sid.2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"

    def test_dotted_session_id_rejected(self) -> None:
        """A dotted session_id would inject extra NATS subject segments."""
        with pytest.raises(ValueError, match="Invalid routing id"):
            _relay()._sid_hint_key("kai", "evil.injected")

    def test_wildcard_session_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid routing id"):
            _relay()._sid_hint_key("kai", "a>b")

    def test_all_hyphen_session_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid routing id"):
            _relay()._sid_hint_key("kai", "----")
