"""Behavioral tests for ``plugin/hooks/session-end.sh``.

The SessionEnd dispatcher makes a network call to the biff relay
(``biff-hook claude-code session-end``) that normally takes several seconds
and can hang on a dead relay. Claude Code cancels any SessionEnd hook still
running when it tears the process down and prints "Hook cancelled", so the
dispatcher must detach that call and return at once rather than block on it.

These tests run the real script under a temporary ``PATH`` carrying a
deliberately slow ``biff-hook`` stub, against a real enabled git repo, and
assert the parent returns promptly while the detached call still fires.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

_HOOK = Path(__file__).resolve().parent.parent / "plugin" / "hooks" / "session-end.sh"

# The stub sleeps this long. It is well above the parent's acceptable return
# time but short enough to keep the suite fast. The parent must return in a
# small fraction of it; a blocking (undetached) parent would take at least
# this long.
_STUB_SLEEP = 3.0
_PARENT_BUDGET = 1.5


def _git(repo: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )


def _enabled_repo(tmp_path: Path) -> Path:
    """A real git repo with the committed ``.punt-labs/biff/enabled`` marker."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    marker_dir = tmp_path / ".punt-labs" / "biff"
    marker_dir.mkdir(parents=True)
    (marker_dir / "enabled").write_text("")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "enable biff")
    return tmp_path


def _slow_biff_hook_on_path(bin_dir: Path, *, sentinel: Path) -> dict[str, str]:
    """Create a slow ``biff-hook`` stub in *bin_dir*; return an env with it first.

    The stub records that it ran (touches *sentinel*) after sleeping, so a
    test can prove the detached call still fires even though the parent has
    already returned.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "biff-hook"
    stub.write_text(f'#!/usr/bin/env bash\nsleep {_STUB_SLEEP}\ntouch "{sentinel}"\n')
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    return env


def _failing_biff_hook_on_path(bin_dir: Path, *, home: Path) -> dict[str, str]:
    """Run the real hook entry point with an injected cleanup failure.

    Point ``HOME`` at the sandbox to exercise the child's private sink.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "biff-hook"
    stub.write_text(
        f"#!{sys.executable}\n"
        "from unittest.mock import patch\n"
        "from biff._hook_runtime import main\n"
        "with patch('biff.hook.handle_session_end',\n"
        "           side_effect=OSError('stub boom')):\n"
        "    main()\n"
    )
    stub.chmod(0o755)
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["HOME"] = str(home)
    return env


def _run_hook(repo: Path, env: dict[str, str]) -> float:
    """Run session-end.sh from *repo*; return the parent's wall-clock seconds."""
    start = time.monotonic()
    # Run the script by its own shebang (it is executable) rather than via a
    # partial "bash" path (ruff S607); _HOOK is an absolute path.
    subprocess.run(  # noqa: S603
        [str(_HOOK)],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=_STUB_SLEEP * 3,
    )
    return time.monotonic() - start


class TestSessionEndDetaches:
    """The dispatcher must not block the exit on the relay call."""

    def test_parent_returns_before_slow_hook_finishes(self, tmp_path: Path) -> None:
        """The script returns well before the slow biff-hook stub completes."""
        repo = _enabled_repo(tmp_path / "repo")
        sentinel = tmp_path / "ran"
        env = _slow_biff_hook_on_path(tmp_path / "bin", sentinel=sentinel)

        elapsed = _run_hook(repo, env)

        assert elapsed < _PARENT_BUDGET, (
            f"session-end.sh blocked for {elapsed:.2f}s on the relay call; "
            f"it must detach and return under {_PARENT_BUDGET}s (#471)"
        )

    def test_detached_call_still_fires(self, tmp_path: Path) -> None:
        """Detaching must not drop the call — the stub still runs in the background."""
        repo = _enabled_repo(tmp_path / "repo")
        sentinel = tmp_path / "ran"
        env = _slow_biff_hook_on_path(tmp_path / "bin", sentinel=sentinel)

        _run_hook(repo, env)
        # The parent has returned; the detached stub is still sleeping. Wait
        # out its sleep plus a margin and confirm it actually executed.
        deadline = time.monotonic() + _STUB_SLEEP * 2
        while time.monotonic() < deadline:
            if sentinel.exists():
                break
            time.sleep(0.05)
        assert sentinel.exists(), (
            "the detached biff-hook call never ran — detaching dropped the "
            "session-end relay call entirely"
        )


class TestSessionEndObservability:
    """A failed detached cleanup must still leave a breadcrumb."""

    def test_failed_detached_call_is_logged(self, tmp_path: Path) -> None:
        """The child records a cleanup failure despite stderr being discarded."""
        repo = _enabled_repo(tmp_path / "repo")
        home = tmp_path / "home"
        env = _failing_biff_hook_on_path(tmp_path / "bin", home=home)

        _run_hook(repo, env)

        log = home / ".punt-labs" / "biff" / "logs" / "biff.log"
        deadline = time.monotonic() + _STUB_SLEEP
        while time.monotonic() < deadline:
            if log.exists() and "Session cleanup failed" in log.read_text():
                break
            time.sleep(0.05)
        assert log.exists(), "the detached child left no durable diagnostic"
        diagnostics = log.read_text()
        assert "Starting session cleanup: pid=" in diagnostics
        assert "Session cleanup failed: pid=" in diagnostics
        assert "stub boom" not in diagnostics
        assert not (home / ".punt-labs" / "biff" / "hook-errors.log").exists()


class TestSessionEndGatesUnchanged:
    """Detaching must not weaken the no-op gates (tool-enable-disable.md)."""

    def test_no_marker_is_noop_and_fast(self, tmp_path: Path) -> None:
        """A repo without the enabled marker never calls biff-hook."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test")
        _git(repo, "commit", "-q", "--allow-empty", "-m", "no marker")
        sentinel = tmp_path / "ran"
        env = _slow_biff_hook_on_path(tmp_path / "bin", sentinel=sentinel)

        elapsed = _run_hook(repo, env)

        assert elapsed < _PARENT_BUDGET
        time.sleep(_STUB_SLEEP + 0.5)
        assert not sentinel.exists(), (
            "biff-hook ran despite no enabled marker — the gate was bypassed"
        )
