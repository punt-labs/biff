"""The runs of a session: the current one, and those a newer one replaced."""

from __future__ import annotations

from typing import Self, final

from biff.daemon.refusal import RefusalError
from biff.frame_reply import ErrorCode

__all__ = ["RunLedger"]

_MAX_RETIRED_RUNS = 64


@final
class RunLedger:
    """Which run of a session is current, and which were replaced for good.

    A run is replaced by a newer one and may never come back: a replaced run that
    obeys "reconnect" must start a new run, not evict the current one.  Only the
    most recent replaced runs are remembered.
    """

    __slots__ = ("_current", "_retired")

    _current: str
    _retired: dict[str, None]

    def __new__(cls, run: str) -> Self:
        """Start with *run* current ("" when the first client claims none)."""
        self = super().__new__(cls)
        self._current = run
        self._retired = {}
        return self

    @property
    def current(self) -> str:
        """The main agent id of the session's current run."""
        return self._current

    def admit(self, session_id: str, run: str) -> str:
        """Record a connection claiming *run*; return the run it replaced, or "".

        A client that claims no run neither replaces nor is replaced.

        Raises:
            RefusalError: *run* is the id of a run that was replaced.
        """
        if run in self._retired:
            msg = (
                f"run {run} of session {session_id} was replaced by a newer "
                "run and cannot come back; start a new run with a fresh run id"
            )
            raise RefusalError.of(ErrorCode.SESSION_REPLACED, msg)
        if not run or run == self._current:
            return ""
        replaced, self._current = self._current, run
        if replaced:
            self._retired[replaced] = None
            while len(self._retired) > _MAX_RETIRED_RUNS:
                del self._retired[next(iter(self._retired))]
        return replaced
