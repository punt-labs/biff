"""The start-up warning that biff channels cannot be live (DES-087)."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from typing import Self, cast, final

from biff.plugins.claude_code.channel_gates import (
    ChannelGate,
    ClosedGate,
    FeatureFlagGate,
)
from biff.plugins.claude_code.launch_flag import LaunchFlagGate
from biff.plugins.claude_code.managed_policy import ManagedPolicyGate

__all__ = ["StartupWarning"]

logger = logging.getLogger(__name__)


@final
class StartupWarning:
    """Say that channels are closed, why, how to open them, and how to poll.

    The warning is built from every gate a hook can see without the model: the
    feature-flag opt-outs, the managed policy and the launch flag.  A gate whose
    state is unknown stays silent.
    """

    __slots__ = ("_gates",)

    _gates: tuple[ChannelGate, ...]

    def __new__(cls, gates: tuple[ChannelGate, ...]) -> Self:
        """Check *gates*, in order."""
        self = super().__new__(cls)
        self._gates = gates
        return self

    @classmethod
    def for_machine(
        cls,
        env: Mapping[str, str] | None = None,
        platform: str | None = None,
    ) -> Self:
        """Return the warning for this process's environment and operating system."""
        env = os.environ if env is None else env
        platform = sys.platform if platform is None else platform
        return cls(
            (
                *FeatureFlagGate.all_in(env),
                ManagedPolicyGate(ManagedPolicyGate.directory_for(platform)),
                LaunchFlagGate(env),
            )
        )

    @classmethod
    def add_to(cls, output: dict[str, object]) -> dict[str, object]:
        """Return SessionStart *output* with this machine's warning added."""
        return cls.for_machine().apply(output)

    def apply(self, output: dict[str, object]) -> dict[str, object]:
        """Return *output* carrying the warning to the user and the model.

        The model reads it in ``additionalContext`` and the user in
        ``systemMessage``; *output* is returned unchanged when no gate is closed.
        """
        text = self.text()
        if text is None:
            return output
        specific = cast("dict[str, object]", output["hookSpecificOutput"])
        context = f"{specific['additionalContext']} {text}"
        return {
            **output,
            "hookSpecificOutput": {**specific, "additionalContext": context},
            "systemMessage": text,
        }

    def text(self) -> str | None:
        """Return the warning, or ``None`` when every gate is open.

        A gate that raises is logged and skipped: this runs while a session
        starts and must neither break it nor hide what the other gates found.
        """
        closed: list[ClosedGate] = []
        for gate in self._gates:
            try:
                closed.extend(gate.closed())
            except Exception:
                logger.warning(
                    "channel gate %s failed", type(gate).__name__, exc_info=True
                )
        if not closed:
            return None
        reasons = "; ".join(gate.reason for gate in closed)
        fixes = " ".join(gate.fix for gate in closed)
        return (
            f"biff: channels are not live in this session ({reasons}). "
            "New messages will not arrive on their own. "
            f"Run /biff:read 2m to poll. {fixes}"
        )
