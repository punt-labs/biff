"""Tests for Claude session-id routing identity."""

from __future__ import annotations

import logging
import os
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


def _always_live_ancestor(_pid: int) -> bool:
    return True


def _never_live_ancestor(_pid: int) -> bool:
    return False


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


class TestResolveClaudePidFromEnv:
    """``CLAUDE_PID`` parsing -- the write-side and read-side shared key."""

    def test_valid_pid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_PID", "12345")
        assert sid_mod._claude_pid_from_env() == 12345

    def test_absent_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_PID", raising=False)
        assert sid_mod._claude_pid_from_env() is None

    def test_empty_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_PID", "")
        assert sid_mod._claude_pid_from_env() is None

    def test_non_integer_returns_none_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("CLAUDE_PID", "not-a-pid")
        with caplog.at_level(logging.WARNING, logger="biff.session_id"):
            assert sid_mod._claude_pid_from_env() is None
        assert any(
            "CLAUDE_PID" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_negative_pid_returns_none_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A negative value parses as a valid int but is not a real PID --
        psutil.Process(pid<=0) raises ValueError, which _process_start_time
        does not catch, so this must be rejected here rather than let
        through to crash the SessionStart hook.
        """
        monkeypatch.setenv("CLAUDE_PID", "-1")
        with caplog.at_level(logging.WARNING, logger="biff.session_id"):
            assert sid_mod._claude_pid_from_env() is None
        assert any(
            "CLAUDE_PID" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_zero_pid_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_PID", "0")
        assert sid_mod._claude_pid_from_env() is None


class TestResolveClaudePid:
    """``_resolve_claude_pid`` write-side fallback chain (explicit
    ``is not None`` checks, not an ``or`` chain -- see DES-058)."""

    def test_prefers_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_PID", "12345")

        def _fail(*_args: object, **_kwargs: object) -> int:
            raise AssertionError("topmost_claude_pid should not be called")

        monkeypatch.setattr(sid_mod, "topmost_claude_pid", _fail)
        assert sid_mod._resolve_claude_pid() == 12345

    def test_falls_back_to_walk_when_env_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_PID", raising=False)
        monkeypatch.setattr(sid_mod, "topmost_claude_pid", lambda: 54321)
        assert sid_mod._resolve_claude_pid() == 54321

    def test_falls_back_to_getppid_when_both_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_PID", raising=False)
        monkeypatch.setattr(sid_mod, "topmost_claude_pid", lambda: None)
        monkeypatch.setattr(os, "getppid", lambda: 111)
        assert sid_mod._resolve_claude_pid() == 111


class TestResolveRoutingIdEnvVar:
    """``CLAUDE_PID`` (not the process-tree walk) keys the hint-file lookup.

    Regression coverage: a nested claude process (explicit
    ``claude -p``, or one spawned automatically by another plugin's own
    hook) walked to the same topmost ancestor PID as its parent under the
    old mechanism and overwrote the parent's hint file with its own
    session_id. ``CLAUDE_PID`` is delivered directly and distinctly to
    each claude process by Claude Code, so nested and parent sessions key
    their hint files under different PIDs -- nothing to clobber.

    Earlier draft of this fix returned ``CLAUDE_CODE_SESSION_ID`` directly
    instead of using it to pick a hint file. That breaks across ``/clear``:
    the long-lived MCP server's own copy of that env var is frozen at
    process-spawn time, so it goes stale the moment ``/clear`` mints a new
    session_id, silently reopening the exact same bug through a different
    door (the hint file, by contrast, is rewritten fresh on every
    SessionStart, including a ``/clear``-sourced one).
    """

    def test_claude_pid_selects_the_write_side_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The nested and parent sessions, sharing a topmost ancestor PID
        under the old mechanism, now write to *different* files because
        each reads its own ``CLAUDE_PID`` -- no clobbering is possible.
        """
        _use_tmp_data_dir(monkeypatch, tmp_path)

        def _fail(*_args: object, **_kwargs: object) -> int:
            raise AssertionError(
                "topmost_claude_pid should not be called when CLAUDE_PID is set"
            )

        monkeypatch.setattr(sid_mod, "topmost_claude_pid", _fail)
        monkeypatch.setattr(sid_mod, "_process_start_time", _fixed_start_time(111.0))
        monkeypatch.setattr(sid_mod, "is_live_ancestor", _always_live_ancestor)

        # Parent session captures its own hint under its own CLAUDE_PID.
        monkeypatch.setenv("CLAUDE_PID", "39417")
        SessionHint.capture("732f03a9-cd63-4eda-a3db-f2e8fe66c9ea", "startup").write()

        # Nested session captures its own hint under ITS OWN CLAUDE_PID --
        # distinct from the parent's, unlike the old topmost-ancestor walk.
        monkeypatch.setenv("CLAUDE_PID", "39900")
        SessionHint.capture("55818b07-466d-45e2-8e99-b61a98a3279b", "startup").write()

        # The parent, resolving again under its own CLAUDE_PID, still gets
        # its own unclobbered id.
        monkeypatch.setenv("CLAUDE_PID", "39417")
        assert (
            SessionHint.resolve_routing_id() == "732f03a9-cd63-4eda-a3db-f2e8fe66c9ea"
        )

    def test_claude_pid_used_instead_of_process_tree_walk(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _use_tmp_data_dir(monkeypatch, tmp_path)

        def _fail(*_args: object, **_kwargs: object) -> int:
            raise AssertionError("topmost_claude_pid should not be called")

        monkeypatch.setattr(sid_mod, "topmost_claude_pid", _fail)
        monkeypatch.setattr(sid_mod, "_process_start_time", _fixed_start_time(111.0))
        monkeypatch.setattr(sid_mod, "is_live_ancestor", _always_live_ancestor)
        monkeypatch.setenv("CLAUDE_PID", "57369")
        SessionHint(
            session_id="2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b",
            claude_pid=57369,
            claude_start_time=111.0,
            source="startup",
        ).write()
        assert (
            SessionHint.resolve_routing_id() == "2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"
        )

    def test_env_pid_failing_live_ancestor_corroboration_falls_back_to_walk(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The PID-recycling scenario DES-058's corroboration step exists
        for: this process's own long-frozen ``CLAUDE_PID`` names a PID
        that is currently live but is NOT (any longer) this process's
        ancestor -- e.g. the real ancestor died and an unrelated,
        legitimate claude session was later assigned that PID by the OS.
        Corroboration must fail and route through the walk instead of
        trusting the stale env value.
        """
        _use_tmp_data_dir(monkeypatch, tmp_path)
        monkeypatch.setattr(sid_mod, "_process_start_time", _fixed_start_time(111.0))
        monkeypatch.setattr(sid_mod, "is_live_ancestor", _never_live_ancestor)
        monkeypatch.setattr(sid_mod, "topmost_claude_pid", lambda: 57369)
        monkeypatch.setenv("CLAUDE_PID", "39417")  # stale -- not a live ancestor

        # The (correct, walk-resolved) hint, unrelated to the stale env pid.
        SessionHint(
            session_id="2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b",
            claude_pid=57369,
            claude_start_time=111.0,
            source="startup",
        ).write()
        assert (
            SessionHint.resolve_routing_id() == "2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"
        )

    def test_env_pid_failing_corroboration_logs_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _use_tmp_data_dir(monkeypatch, tmp_path)
        monkeypatch.setattr(sid_mod, "is_live_ancestor", _never_live_ancestor)
        monkeypatch.setattr(sid_mod, "topmost_claude_pid", lambda: None)
        monkeypatch.setenv("CLAUDE_PID", "39417")
        with caplog.at_level(logging.WARNING, logger="biff.session_id"):
            SessionHint.resolve_routing_id()
        assert any(
            "CLAUDE_PID" in r.message
            and "live ancestors" in r.message
            and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_malformed_claude_pid_falls_back_to_process_tree_walk(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _use_tmp_data_dir(monkeypatch, tmp_path)
        monkeypatch.setenv("CLAUDE_PID", "not-a-pid")
        monkeypatch.setattr(sid_mod, "topmost_claude_pid", lambda: 57369)
        monkeypatch.setattr(sid_mod, "_process_start_time", _fixed_start_time(111.0))
        SessionHint(
            session_id="2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b",
            claude_pid=57369,
            claude_start_time=111.0,
            source="startup",
        ).write()
        assert (
            SessionHint.resolve_routing_id() == "2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"
        )

    def test_survives_clear_unlike_the_frozen_session_id_env_var(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``/clear`` mints a new session_id but does not spawn a new
        process, so ``CLAUDE_PID`` is unchanged -- the hint file at that
        PID gets rewritten by the fresh SessionStart(source=clear) hook
        invocation, and a subsequent resolve reads the new id straight
        through, with no stale process-local cache anywhere in the path.
        """
        _use_tmp_data_dir(monkeypatch, tmp_path)
        monkeypatch.setattr(sid_mod, "_process_start_time", _fixed_start_time(111.0))
        monkeypatch.setattr(sid_mod, "is_live_ancestor", _always_live_ancestor)
        monkeypatch.setenv("CLAUDE_PID", "39417")

        SessionHint.capture("732f03a9-cd63-4eda-a3db-f2e8fe66c9ea", "startup").write()
        assert (
            SessionHint.resolve_routing_id() == "732f03a9-cd63-4eda-a3db-f2e8fe66c9ea"
        )

        # /clear: same process (CLAUDE_PID unchanged), fresh session_id,
        # a new SessionStart(source="clear") hook invocation rewrites the
        # hint at the SAME path.
        SessionHint.capture("9e2c9777-74ba-4422-ab96-7fc6f02dd11b", "clear").write()
        assert (
            SessionHint.resolve_routing_id() == "9e2c9777-74ba-4422-ab96-7fc6f02dd11b"
        )

    def test_rejected_design_would_have_gone_stale_across_clear(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct reproduction of the rejected direct-env-var-return
        design's failure mode: a value captured once (as it would be by a
        long-lived server reading its own frozen ``os.environ`` copy) does
        not observe a later change to that same variable in the current
        process's environment -- because Claude Code's ``/clear`` updates
        ``CLAUDE_CODE_SESSION_ID`` in its *own* process only, never in an
        already-spawned MCP server's. The env var itself is real and
        genuinely updates within a process that re-reads it fresh each
        time; the failure is specific to capturing it once and holding on.
        """
        monkeypatch.setenv(
            "CLAUDE_CODE_SESSION_ID", "732f03a9-cd63-4eda-a3db-f2e8fe66c9ea"
        )
        captured_once = os.environ.get("CLAUDE_CODE_SESSION_ID")  # server startup

        monkeypatch.setenv(  # /clear, in Claude Code's own process
            "CLAUDE_CODE_SESSION_ID", "9e2c9777-74ba-4422-ab96-7fc6f02dd11b"
        )

        assert captured_once == "732f03a9-cd63-4eda-a3db-f2e8fe66c9ea"  # stale
        assert (
            os.environ.get("CLAUDE_CODE_SESSION_ID")
            == "9e2c9777-74ba-4422-ab96-7fc6f02dd11b"
        )  # live value has moved on -- captured_once no longer agrees with it


class TestResolveRoutingId:
    """Server-side resolution: hint -> routing id, with the recycle guard.

    These exercise the PID-walk fallback -- the autouse
    ``_clear_claude_session_env`` fixture keeps ``CLAUDE_PID`` unset so the
    env-var-first branch never short-circuits them.
    """

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
