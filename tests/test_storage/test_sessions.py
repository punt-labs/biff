"""Tests for JSON session storage."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from biff.models import UserSession
from biff.storage.sessions import SessionStore


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(data_dir=tmp_path)


class TestUpdate:
    def test_creates_data_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b"
        store = SessionStore(data_dir=nested)
        session = UserSession(user="kai", plan="coding")
        store.update(session)
        assert (nested / "sessions.json").exists()

    def test_create_new_session(self, store: SessionStore) -> None:
        session = UserSession(user="kai", plan="refactoring auth")
        store.update(session)
        result = store.get_user("kai")
        assert result is not None
        assert result.plan == "refactoring auth"

    def test_update_existing_session(self, store: SessionStore) -> None:
        store.update(UserSession(user="kai", plan="old plan"))
        store.update(UserSession(user="kai", plan="new plan"))
        result = store.get_user("kai")
        assert result is not None
        assert result.plan == "new plan"

    def test_preserves_other_sessions(self, store: SessionStore) -> None:
        store.update(UserSession(user="kai", plan="kai's plan"))
        store.update(UserSession(user="eric", plan="eric's plan"))
        kai = store.get_user("kai")
        eric = store.get_user("eric")
        assert kai is not None and kai.plan == "kai's plan"
        assert eric is not None and eric.plan == "eric's plan"


class TestGetUser:
    def test_missing_user(self, store: SessionStore) -> None:
        assert store.get_user("nobody") is None

    def test_returns_full_session(self, store: SessionStore) -> None:
        session = UserSession(user="kai", plan="testing", biff_enabled=False)
        store.update(session)
        result = store.get_user("kai")
        assert result is not None
        assert result.user == "kai"
        assert result.plan == "testing"
        assert result.biff_enabled is False


class TestGetActive:
    def test_empty_store(self, store: SessionStore) -> None:
        assert store.get_active() == []

    def test_filters_by_ttl(self, store: SessionStore) -> None:
        now = datetime.now(UTC)
        recent = UserSession(user="kai", last_active=now)
        stale = UserSession(
            user="eric",
            last_active=now - timedelta(seconds=300),
        )
        store.update(recent)
        store.update(stale)
        active = store.get_active(ttl=120)
        assert len(active) == 1
        assert active[0].user == "kai"

    def test_custom_ttl(self, store: SessionStore) -> None:
        now = datetime.now(UTC)
        session = UserSession(
            user="kai",
            last_active=now - timedelta(seconds=60),
        )
        store.update(session)
        assert len(store.get_active(ttl=30)) == 0
        assert len(store.get_active(ttl=120)) == 1


class TestHeartbeat:
    def test_creates_new_session(self, store: SessionStore) -> None:
        store.heartbeat("kai")
        result = store.get_user("kai")
        assert result is not None
        assert result.user == "kai"
        assert result.plan == ""

    def test_updates_last_active(self, store: SessionStore) -> None:
        old_time = datetime.now(UTC) - timedelta(seconds=300)
        store.update(UserSession(user="kai", plan="coding", last_active=old_time))
        store.heartbeat("kai")
        result = store.get_user("kai")
        assert result is not None
        assert result.last_active > old_time

    def test_preserves_plan(self, store: SessionStore) -> None:
        store.update(UserSession(user="kai", plan="refactoring"))
        store.heartbeat("kai")
        result = store.get_user("kai")
        assert result is not None
        assert result.plan == "refactoring"

    def test_preserves_biff_enabled(self, store: SessionStore) -> None:
        store.update(UserSession(user="kai", biff_enabled=False))
        store.heartbeat("kai")
        result = store.get_user("kai")
        assert result is not None
        assert result.biff_enabled is False


class TestMalformedData:
    def test_corrupt_json_returns_empty(
        self, store: SessionStore, tmp_path: Path
    ) -> None:
        (tmp_path / "sessions.json").write_text("not json at all")
        assert store._read_all() == {}

    def test_missing_file_returns_empty(self, store: SessionStore) -> None:
        assert store._read_all() == {}

    def test_valid_json_with_file(self, store: SessionStore, tmp_path: Path) -> None:
        session = UserSession(user="kai", plan="testing")
        data = {"kai": session.model_dump(mode="json")}
        (tmp_path / "sessions.json").write_text(json.dumps(data))
        result = store._read_all()
        assert "kai" in result
        assert result["kai"].plan == "testing"
