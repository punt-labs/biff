"""The joint launch-flag and policy gate: when can biff be a live channel."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Self, final

import psutil

from biff.caller_stamp import main_agent_id
from biff.plugins.claude_code.channel_gates import ClosedGate
from biff.plugins.claude_code.claude_argv import (
    BIFF_CHANNEL,
    CHANNELS_FLAG,
    DEV_FLAG,
    ClaudeArgv,
)
from biff.plugins.claude_code.managed_policy import ManagedPolicy

__all__ = ["LaunchFlagGate"]

logger = logging.getLogger(__name__)

type ArgvReader = Callable[[int], Sequence[str]]

_RESTART = (
    f"Restart Claude Code with {DEV_FLAG} {BIFF_CHANNEL}. "
    f"Organizations that list biff in allowedChannelPlugins can use "
    f"{CHANNELS_FLAG} {BIFF_CHANNEL} instead."
)


@final
class LaunchFlagGate:
    """Open only when ``claude`` was started with a way to register biff.

    Biff is a live channel when either the development flag names biff, which
    bypasses ``allowedChannelPlugins``, or ``--channels`` names biff and the
    managed policy lists biff, because ``--channels`` alone only accepts
    Anthropic's own list.  A ``-p`` run ignores channels.

    Neither the environment nor a hook's stdin carries the flags, but
    ``CLAUDE_PID`` is the ``claude`` process, whose argv does.  It is trusted
    only when it is a live ancestor of this hook.  When it cannot be, or its
    argv cannot be read, the gate is unknown, not closed: it stays silent and
    logs, so a user is never warned falsely.
    """

    __slots__ = ("_ancestors", "_env", "_policy", "_read_argv")

    _ancestors: Callable[[], frozenset[int]] | None
    _env: Mapping[str, str]
    _policy: ManagedPolicy | None
    _read_argv: ArgvReader

    def __new__(
        cls,
        env: Mapping[str, str],
        policy: ManagedPolicy | None,
        read_argv: ArgvReader | None = None,
        ancestors: Callable[[], frozenset[int]] | None = None,
    ) -> Self:
        """Look up ``CLAUDE_PID`` in *env*, read its argv, and judge it by *policy*."""
        self = super().__new__(cls)
        self._env = env
        self._policy = policy
        self._read_argv = cls._process_argv if read_argv is None else read_argv
        self._ancestors = ancestors
        return self

    @staticmethod
    def _process_argv(pid: int) -> Sequence[str]:
        return psutil.Process(pid).cmdline()

    def closed(self) -> tuple[ClosedGate, ...]:
        """Return the closed gate when biff cannot be registered as a channel."""
        argv = self._argv()
        if argv is None:
            return (
                ClosedGate(
                    reason="launch flags could not be checked",
                    fix="",
                    notify_user=False,
                    unknown=True,
                ),
            )
        if argv.is_print:
            return (
                ClosedGate(
                    reason="Claude Code is running with -p, which ignores channels",
                    fix="",
                    notify_user=False,
                ),
            )
        if argv.dev_names_biff:
            return ()
        if argv.channels_name_biff:
            policy = self._policy
            if policy is not None and policy.lists_biff:
                return ()
            return (
                ClosedGate(
                    reason=(
                        "allowedChannelPlugins for biff is not visible locally; "
                        "a server-managed policy may still allow channels"
                    ),
                    fix="",
                    notify_user=False,
                    unknown=True,
                ),
            )
        return (
            ClosedGate(
                reason="Claude Code was not started with a channel flag for biff",
                fix=_RESTART,
            ),
        )

    def _argv(self) -> ClaudeArgv | None:
        """Return Claude Code's parsed argv, or ``None`` (logged) when unknown."""
        try:
            corroborate = None if self._ancestors is None else self._ancestors()
            pid = main_agent_id(self._env, ancestors=corroborate)
        except (psutil.Error, OSError) as exc:
            logger.info(
                "launch_flag_unknown: ancestors unreadable: %s", type(exc).__name__
            )
            return None
        if pid is None:
            logger.info("launch_flag_unknown: CLAUDE_PID is not a live ancestor")
            return None
        try:
            return ClaudeArgv(self._read_argv(int(pid)))
        except (psutil.Error, OSError) as exc:
            logger.info(
                "launch_flag_unknown: argv of pid %s unreadable: %s",
                pid,
                type(exc).__name__,
            )
            return None
