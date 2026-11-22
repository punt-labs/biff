"""The installed ``biff-hook`` script runs the hook dispatcher.

Every hook script runs ``biff-hook ... 2>/dev/null || true``, so a console
script that points at a wrong module would make the hooks silently do nothing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.subprocess


def test_unknown_event_exits_1_with_message() -> None:
    script = Path(sys.executable).parent / "biff-hook"
    result = subprocess.run(  # noqa: S603
        [str(script), "claude-code", "no-such-event"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 1
    assert "Unknown claude-code event" in result.stderr
