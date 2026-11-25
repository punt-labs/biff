"""``biff doctor``'s Claude Code checks: the status line, and what blocks channels.

Claude Code drops biff's channel without a word when a feature-flag opt-out or
a cloud provider is set, so doctor names each one with its fix.  Every check is
informational: polling still delivers messages.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Self, final

from biff.plugins.claude_code.channel_gates import EnvGate
from biff.plugins.claude_code.statusline import read_settings
from biff.plugins.contract import CheckResult

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["DoctorChecks"]


@final
class DoctorChecks:
    """Claude Code's doctor checks for one user settings file and environment."""

    __slots__ = ("_gates", "_settings")

    _gates: tuple[EnvGate, ...]
    _settings: Path

    def __new__(cls, settings: Path, env: Mapping[str, str]) -> Self:
        """Check *settings* (``settings.json``) and *env*, Claude Code's shell."""
        self = super().__new__(cls)
        self._settings = settings
        self._gates = (*EnvGate.opt_outs(env), EnvGate.provider(env))
        return self

    def all(self) -> tuple[CheckResult, ...]:
        """Return the status-line check, then the channel checks."""
        return (self.statusline(), *self.channels())

    def statusline(self) -> CheckResult:
        """Report whether biff's status line is configured."""
        try:
            settings = read_settings(self._settings)
        except (json.JSONDecodeError, OSError):
            return self._result("Status line", "could not read settings.json")
        status_line = settings.get("statusLine")
        if isinstance(status_line, dict):
            cmd = status_line.get("command")  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
            if isinstance(cmd, str) and "biff statusline" in cmd:
                return self._result("Status line", "configured", passed=True)
        return self._result(
            "Status line", "not configured (restart Claude Code to auto-install)"
        )

    def channels(self) -> tuple[CheckResult, ...]:
        """Return one failed check per closed gate, or one passed check."""
        closed = [gate for env_gate in self._gates for gate in env_gate.closed()]
        if not closed:
            message = (
                "no variable in this shell blocks channels "
                "(Claude Code's own environment is checked at its start-up)"
            )
            return (self._result("Channels", message, passed=True),)
        return tuple(
            self._result("Channels", f"{gate.reason}. {gate.fix}") for gate in closed
        )

    @staticmethod
    def _result(name: str, message: str, *, passed: bool = False) -> CheckResult:
        return CheckResult(name=name, passed=passed, message=message, required=False)
