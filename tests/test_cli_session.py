"""Tests for the CLI session lifecycle (biff.cli_session).

Tests the ``CliContext`` dataclass and session lifecycle basics
without requiring NATS.
"""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.models import BiffConfig


class TestCliContext:
    def test_frozen(self) -> None:
        ctx = CliContext(
            relay=object(),  # type: ignore[arg-type]
            config=BiffConfig(user="kai", repo_name="test"),
            session_key="kai:abc12345",
            user="kai",
            tty="abc12345",
            tty_name="tty1",
        )
        assert ctx.user == "kai"
        assert ctx.tty_name == "tty1"
        assert ctx.session_key == "kai:abc12345"

    def test_default_tty_name(self) -> None:
        ctx = CliContext(
            relay=object(),  # type: ignore[arg-type]
            config=BiffConfig(user="kai", repo_name="test"),
            session_key="kai:abc12345",
            user="kai",
            tty="abc12345",
        )
        assert ctx.tty_name == ""
