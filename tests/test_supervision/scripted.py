"""A scripted stand-in for the process boundary of the service manager."""

from __future__ import annotations

import subprocess
from collections import deque
from typing import TYPE_CHECKING, Self, final

from biff.supervision.runner import Supervisor

if TYPE_CHECKING:
    import pytest

type _Outcome = deque[subprocess.CompletedProcess[str]] | Exception


@final
class ScriptedSupervisor:
    """Answers each supervisor command from rules; records every call.

    A rule is an argv prefix and what to do: give a result, give a sequence of
    results (the last repeats), or raise.  The first matching rule wins and an
    unmatched command succeeds with empty output.
    """

    __slots__ = ("_calls", "_rules", "_timeouts")

    _calls: list[tuple[str, ...]]
    _timeouts: list[float]
    _rules: list[tuple[tuple[str, ...], _Outcome]]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._calls = []
        self._rules = []
        self._timeouts = []
        return self

    @property
    def calls(self) -> list[tuple[str, ...]]:
        """Every command run so far, in order."""
        return self._calls

    @property
    def timeouts(self) -> list[float]:
        """The time limit each command was run with, in order."""
        return self._timeouts

    def ran(self, *prefix: str) -> bool:
        """Return whether a command starting with *prefix* was run."""
        return any(call[: len(prefix)] == prefix for call in self._calls)

    def timeout_of(self, *prefix: str) -> float:
        """Return the time limit of the last command starting with *prefix*."""
        return self._timeouts[self.index(*prefix)]

    def index(self, *prefix: str) -> int:
        """Return the position of the last command starting with *prefix*."""
        positions = [
            i for i, call in enumerate(self._calls) if call[: len(prefix)] == prefix
        ]
        return positions[-1]

    def answer(self, *prefix: str, rc: int = 0, out: str = "", err: str = "") -> Self:
        """Make commands starting with *prefix* exit *rc* with this output."""
        done = subprocess.CompletedProcess(prefix, rc, out, err)
        self._rules.append((prefix, deque([done])))
        return self

    def answer_in_turn(self, *prefix: str, rcs: list[int]) -> Self:
        """Answer *prefix* with each exit code in turn; the last one repeats."""
        queue = deque(subprocess.CompletedProcess(prefix, rc, "", "") for rc in rcs)
        self._rules.append((prefix, queue))
        return self

    def raises(self, *prefix: str, error: Exception) -> Self:
        """Make commands starting with *prefix* raise *error*."""
        self._rules.append((prefix, error))
        return self

    def run(
        self, *argv: str, timeout: float = 30.0
    ) -> subprocess.CompletedProcess[str]:
        """Stand in for ``Supervisor.run``."""
        self._calls.append(argv)
        self._timeouts.append(timeout)
        for prefix, outcome in self._rules:
            if argv[: len(prefix)] != prefix:
                continue
            if isinstance(outcome, Exception):
                raise outcome
            done = outcome[0] if len(outcome) == 1 else outcome.popleft()
            return subprocess.CompletedProcess(
                argv, done.returncode, done.stdout, done.stderr
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    def install(self, monkeypatch: pytest.MonkeyPatch) -> Self:
        """Route ``Supervisor.run`` through this script."""
        monkeypatch.setattr(Supervisor, "run", staticmethod(self.run))
        return self
