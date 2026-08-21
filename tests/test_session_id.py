"""Tests for Claude session-id routing identity."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import biff.session_id as sid_mod
from biff.session_id import SessionHint
from biff.tty import validate_routing_id

if TYPE_CHECKING:
    import pytest


def _use_tmp_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sid_mod, "biff_data_dir", lambda: tmp_path)


def _fixed_start_time(value: float) -> Callable[[int], float]:
    """Return a ``_process_start_time`` stub that always reports *value*."""

    def _inner(_pid: int) -> float:
        return value

    return _inner


class TestDeriveRoutingId:
    """derive == deterministic, injective pairing (spec RID == SESSION x KIND)."""

    def test_deterministic(self) -> None:
        a = SessionHint.derive_routing_id("sid-abc", "human")
        b = SessionHint.derive_routing_id("sid-abc", "human")
        assert a == b

    def test_stable_across_resume(self) -> None:
        """The same session_id (a resume) derives the same companion id."""
        before = SessionHint.derive_routing_id("sid-abc", "eric")
        after = SessionHint.derive_routing_id("sid-abc", "eric")
        assert before == after

    def test_distinct_per_role(self) -> None:
        agent = SessionHint.derive_routing_id("sid-abc", "kai")
        human = SessionHint.derive_routing_id("sid-abc", "eric")
        assert agent != human

    def test_distinct_per_session(self) -> None:
        """A fork (fresh session_id) derives a fresh id — no inheritance."""
        parent = SessionHint.derive_routing_id("sid-parent", "eric")
        fork = SessionHint.derive_routing_id("sid-fork", "eric")
        assert parent != fork

    def test_hex_charset_is_valid_routing_id(self) -> None:
        derived = SessionHint.derive_routing_id("sid-abc", "eric")
        assert validate_routing_id(derived) is None

    def test_no_colon_that_would_collide_with_separator(self) -> None:
        assert ":" not in SessionHint.derive_routing_id("sid-abc", "eric")


class TestHintRoundTrip:
    def test_write_then_load(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _use_tmp_data_dir(monkeypatch, tmp_path)
        hint = SessionHint(
            session_id="2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b",
            claude_pid=57369,
            claude_start_time=123456.5,
            source="resume",
        )
        hint.write()
        loaded = SessionHint.load(57369)
        assert loaded == hint

    def test_write_permissions_are_owner_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A durable routing id is never world-readable, whatever the umask.

        A permissive umask (0o000) would let a plain ``write_text`` create the
        file 0o644 and ``mkdir(mode=...)`` yield 0o777; the explicit chmod and
        the ``os.open(..., 0o600)`` create close that window (security review
        P3 completeness).
        """
        import os

        _use_tmp_data_dir(monkeypatch, tmp_path)
        old_umask = os.umask(0o000)
        try:
            SessionHint(
                session_id="2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b",
                claude_pid=57369,
                claude_start_time=1.0,
                source="startup",
            ).write()
        finally:
            os.umask(old_umask)

        sessions_dir = tmp_path / "sessions"
        assert sessions_dir.stat().st_mode & 0o777 == 0o700
        assert (sessions_dir / "57369.json").stat().st_mode & 0o777 == 0o600

    def test_load_absent_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _use_tmp_data_dir(monkeypatch, tmp_path)
        assert SessionHint.load(99999) is None

    def test_load_malformed_json_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _use_tmp_data_dir(monkeypatch, tmp_path)
        (tmp_path / "sessions").mkdir()
        (tmp_path / "sessions" / "42.json").write_text("{not json")
        assert SessionHint.load(42) is None

    def test_load_wrong_shape_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _use_tmp_data_dir(monkeypatch, tmp_path)
        (tmp_path / "sessions").mkdir()
        (tmp_path / "sessions" / "42.json").write_text('{"session_id": 5}')
        assert SessionHint.load(42) is None


class TestCapture:
    def test_captures_pid_and_start_time(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _use_tmp_data_dir(monkeypatch, tmp_path)
        monkeypatch.setattr(sid_mod, "_resolve_claude_pid", lambda: 12345)
        monkeypatch.setattr(sid_mod, "_process_start_time", _fixed_start_time(999.0))
        hint = SessionHint.capture("sid-xyz", "startup")
        assert hint.claude_pid == 12345
        assert hint.claude_start_time == 999.0
        assert hint.session_id == "sid-xyz"
        assert hint.source == "startup"


class TestResolveRoutingId:
    """Server-side resolution: hint -> routing id, with the recycle guard."""

    def test_no_claude_ancestor_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sid_mod, "topmost_claude_pid", lambda: None)
        assert SessionHint.resolve_routing_id() is None

    def test_resume_reuses_session_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A hint whose live pid matches its start time yields the session_id."""
        _use_tmp_data_dir(monkeypatch, tmp_path)
        monkeypatch.setattr(sid_mod, "topmost_claude_pid", lambda: 57369)
        monkeypatch.setattr(sid_mod, "_process_start_time", _fixed_start_time(111.0))
        SessionHint(
            session_id="2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b",
            claude_pid=57369,
            claude_start_time=111.0,
            source="resume",
        ).write()
        assert (
            SessionHint.resolve_routing_id() == "2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"
        )

    def test_recycled_pid_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A leftover hint whose start time no longer matches is rejected."""
        _use_tmp_data_dir(monkeypatch, tmp_path)
        monkeypatch.setattr(sid_mod, "topmost_claude_pid", lambda: 57369)
        # The live process at pid 57369 started at a *different* time.
        monkeypatch.setattr(sid_mod, "_process_start_time", _fixed_start_time(222.0))
        SessionHint(
            session_id="dead-session-id-000000000000000000000",
            claude_pid=57369,
            claude_start_time=111.0,
            source="startup",
        ).write()
        assert SessionHint.resolve_routing_id() is None

    def test_unknown_start_time_never_matches(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A 0.0 stored start-time must not bind a dead id to a recycled PID.

        If capture hit a psutil fault it stored 0.0, and if the live process
        also reads 0.0, a naive ``==`` would compare 0.0 == 0.0 -> True and
        reopen MISROUTE.  The guard rejects 0.0 outright: resolve yields None
        (fresh-hex fallback), never the stale id.
        """
        _use_tmp_data_dir(monkeypatch, tmp_path)
        monkeypatch.setattr(sid_mod, "topmost_claude_pid", lambda: 57369)
        monkeypatch.setattr(sid_mod, "_process_start_time", _fixed_start_time(0.0))
        # A *valid* hex id, so only the recycle guard can reject it — not the
        # routing-id validator (which would mask the guard hole).
        SessionHint(
            session_id="dead0000-0000-0000-0000-000000000000",
            claude_pid=57369,
            claude_start_time=0.0,
            source="startup",
        ).write()
        assert SessionHint.resolve_routing_id() is None

    def test_malformed_session_id_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _use_tmp_data_dir(monkeypatch, tmp_path)
        monkeypatch.setattr(sid_mod, "topmost_claude_pid", lambda: 42)
        monkeypatch.setattr(sid_mod, "_process_start_time", _fixed_start_time(5.0))
        SessionHint(
            session_id="not a valid routing id!",
            claude_pid=42,
            claude_start_time=5.0,
            source="startup",
        ).write()
        assert SessionHint.resolve_routing_id() is None

    def test_absent_hint_under_claude_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _use_tmp_data_dir(monkeypatch, tmp_path)
        monkeypatch.setattr(sid_mod, "topmost_claude_pid", lambda: 42)
        # attempts=1 → single lookup, no retry sleep.
        monkeypatch.setattr(sid_mod, "_RESOLVE_ATTEMPTS", 1)
        assert SessionHint.resolve_routing_id() is None

    def test_under_claude_without_hint_warns(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A broken resume must be observable: under Claude + no hint -> WARNING."""
        _use_tmp_data_dir(monkeypatch, tmp_path)
        monkeypatch.setattr(sid_mod, "topmost_claude_pid", lambda: 42)
        monkeypatch.setattr(sid_mod, "_RESOLVE_ATTEMPTS", 1)
        with caplog.at_level(logging.WARNING, logger="biff.session_id"):
            assert SessionHint.resolve_routing_id() is None
        assert any(
            "resume-reclaim disabled" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_headless_does_not_warn(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The legitimate headless case (no claude ancestor) stays silent."""
        monkeypatch.setattr(sid_mod, "topmost_claude_pid", lambda: None)
        with caplog.at_level(logging.WARNING, logger="biff.session_id"):
            assert SessionHint.resolve_routing_id() is None
        assert caplog.records == []
