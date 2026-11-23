"""The launch-flag gate: Claude Code must be started with a channel flag."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Self, final

import psutil

from biff.plugins.claude_code.channel_gates import ClosedGate

__all__ = ["LaunchFlagGate"]

logger = logging.getLogger(__name__)

_DEV_FLAG = "--dangerously-load-development-channels"
_CHANNELS_FLAG = "--channels"
_BIFF = "plugin:biff@punt-labs"

type ArgvReader = Callable[[int], Sequence[str]]


@final
class LaunchFlagGate:
    """Open while ``claude`` was started with a flag that can register biff.

    Neither the environment nor a hook's stdin carries the launch flags, but
    Claude Code sets ``CLAUDE_PID`` to its own process id, whose argv does.
    When that argv cannot be read the gate is unknown, not closed: it stays
    silent and logs, so a user is never warned falsely.
    """

    __slots__ = ("_env", "_read_argv")

    _env: Mapping[str, str]
    _read_argv: ArgvReader

    def __new__(
        cls, env: Mapping[str, str], read_argv: ArgvReader | None = None
    ) -> Self:
        """Look up ``CLAUDE_PID`` in *env* and read its argv with *read_argv*."""
        self = super().__new__(cls)
        self._env = env
        self._read_argv = cls._process_argv if read_argv is None else read_argv
        return self

    @staticmethod
    def _process_argv(pid: int) -> Sequence[str]:
        return psutil.Process(pid).cmdline()

    def closed(self) -> tuple[ClosedGate, ...]:
        """Return the closed gate when the argv has no usable channel flag."""
        argv = self._argv()
        if argv is None or self._has_channel_flag(argv):
            return ()
        return (
            ClosedGate(
                reason="Claude Code was not started with a channel flag for biff",
                fix=(
                    "Restart Claude Code with "
                    f"{_DEV_FLAG} {_BIFF}, or with {_CHANNELS_FLAG} {_BIFF} "
                    "where your organization allows it."
                ),
            ),
        )

    def _argv(self) -> Sequence[str] | None:
        """Return Claude Code's argv, or ``None`` (logged) when it is unknown."""
        raw = self._env.get("CLAUDE_PID", "")
        try:
            pid = int(raw)
        except ValueError:
            logger.info("launch_flag_unknown: CLAUDE_PID=%r is not a pid", raw)
            return None
        try:
            return self._read_argv(pid)
        except psutil.Error as exc:
            logger.info(
                "launch_flag_unknown: argv of pid %d unreadable: %s",
                pid,
                type(exc).__name__,
            )
            return None

    @staticmethod
    def _has_channel_flag(argv: Sequence[str]) -> bool:
        """Return whether *argv* carries the development flag or names biff."""
        listed = False
        for arg in argv:
            if arg == _DEV_FLAG or arg.startswith(f"{_DEV_FLAG}="):
                return True
            if arg == _CHANNELS_FLAG:
                listed = True
            elif arg.startswith(f"{_CHANNELS_FLAG}="):
                listed = False
                if arg.partition("=")[2] == _BIFF:
                    return True
            elif arg.startswith("-"):
                listed = False
            elif listed and arg == _BIFF:
                return True
        return False
