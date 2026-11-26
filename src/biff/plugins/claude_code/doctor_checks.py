"""``biff doctor``'s Claude Code checks: the status line, and what blocks channels.

Claude Code drops biff's channel without a word when a feature-flag opt-out or
a cloud provider is set, so doctor names each one with its fix.  Every check is
informational: polling still delivers messages.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING, Self, final

from biff.plugins.claude_code.channel_check_report import ChannelCheckReport
from biff.plugins.claude_code.channel_check_target import CheckTarget
from biff.plugins.claude_code.channel_gates import EnvGate
from biff.plugins.claude_code.statusline import read_settings
from biff.plugins.contract import CheckResult

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["DoctorChecks"]


@final
class DoctorChecks:
    """Claude Code's doctor checks for one user settings file and environment."""

    __slots__ = ("_gates", "_settings", "_target", "_user_override")

    _gates: tuple[EnvGate, ...]
    _settings: Path
    _target: Callable[[str | None], CheckTarget]
    _user_override: str | None

    def __new__(
        cls,
        settings: Path,
        env: Mapping[str, str],
        user_override: str | None = None,
        target: Callable[[str | None], CheckTarget] = CheckTarget.from_environment,
    ) -> Self:
        """Check *settings* (``settings.json``) and *env*, Claude Code's shell.

        *user_override* is ``--user``.  *target* names whose sessions the
        end-to-end channel check asks, and where; tests pass a fake relay here.
        """
        self = super().__new__(cls)
        self._settings = settings
        self._user_override = user_override
        self._target = target
        self._gates = (*EnvGate.opt_outs(env), EnvGate.provider(env))
        return self

    def all(self) -> Iterator[CheckResult]:
        """Yield the status line, then the channel gates, then the live check.

        Lazy, so doctor prints the first two before the live check starts to
        wait for the sessions.
        """
        yield self.statusline()
        yield from self.channels()
        yield from self.channel_check()

    def channel_check(self) -> tuple[CheckResult, ...]:
        """Ask each running session whether its channel events reach the model."""
        target = self._target(self._user_override)
        return asyncio.run(ChannelCheckReport(target).results())

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
