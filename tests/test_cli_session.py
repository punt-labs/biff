"""Tests for the CLI session manager (biff.cli_session).

Tests session file TTL logic and persistence without requiring NATS.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from biff.cli_session import _load_session, _save_session, _session_path

if TYPE_CHECKING:
    import pytest


class TestSessionPath:
    def test_path_includes_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("biff.cli_session._SESSION_DIR", tmp_path)
        path = _session_path("my-repo")
        assert path == tmp_path / "my-repo.json"


class TestSaveAndLoad:
    def test_round_trip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("biff.cli_session._SESSION_DIR", tmp_path)
        _save_session("test-repo", "kai", "aabb1122")
        result = _load_session("test-repo")
        assert result is not None
        user, tty = result
        assert user == "kai"
        assert tty == "aabb1122"

    def test_expired_session(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("biff.cli_session._SESSION_DIR", tmp_path)
        # Write a session with old timestamp
        data = {
            "user": "kai",
            "tty": "aabb1122",
            "tty_name": "cli",
            "last_active": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
        }
        path = tmp_path / "test-repo.json"
        path.write_text(json.dumps(data))
        result = _load_session("test-repo")
        assert result is None

    def test_missing_session(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("biff.cli_session._SESSION_DIR", tmp_path)
        result = _load_session("nonexistent")
        assert result is None

    def test_corrupt_session(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("biff.cli_session._SESSION_DIR", tmp_path)
        path = tmp_path / "test-repo.json"
        path.write_text("not json")
        result = _load_session("test-repo")
        assert result is None

    def test_fresh_session_within_ttl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr("biff.cli_session._SESSION_DIR", tmp_path)
        # Write a session with recent timestamp
        data = {
            "user": "kai",
            "tty": "aabb1122",
            "tty_name": "cli",
            "last_active": (datetime.now(UTC) - timedelta(minutes=3)).isoformat(),
        }
        path = tmp_path / "test-repo.json"
        path.write_text(json.dumps(data))
        result = _load_session("test-repo")
        assert result is not None
        assert result == ("kai", "aabb1122")
