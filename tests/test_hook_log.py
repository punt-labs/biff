"""Gate diagnostics from the real hook entry point reach the durable log."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from biff.plugins.claude_code import _hook_runtime, hook


def _no_session(_data: dict[str, object]) -> None:
    return None


@pytest.fixture
def isolated_logging() -> Iterator[None]:
    """Restore the root logger after the entry point reconfigures it."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        yield
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            handler.close()
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)


@pytest.mark.usefixtures("isolated_logging")
def test_gate_info_line_lands_in_the_log_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    monkeypatch.setattr(hook, "_is_biff_enabled", lambda: True)
    monkeypatch.setattr(hook, "_open_session", _no_session)
    monkeypatch.setattr(sys, "argv", ["biff-hook", "claude-code", "session-start"])
    _hook_runtime.main()
    log = (tmp_path / ".punt-labs" / "biff" / "logs" / "biff.log").read_text()
    assert "launch_flag_unknown" in log
