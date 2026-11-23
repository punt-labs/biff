"""The start-up warning that biff channels cannot be live (DES-087)."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Mapping
from typing import Self, cast, final

from biff.plugins.claude_code.channel_gates import ChannelGate, ClosedGate, EnvGate
from biff.plugins.claude_code.launch_flag import LaunchFlagGate
from biff.plugins.claude_code.managed_policy import ManagedPolicy, ManagedPolicyGate
from biff.plugins.claude_code.poll_setting import PollSetting

__all__ = ["StartupWarning"]

logger = logging.getLogger(__name__)


@final
class StartupWarning:
    """Say that channels are closed, why, how to open them, and how to poll.

    The warning is built from every gate a hook can see without the model: the
    feature-flag opt-outs, the cloud provider, the managed policy, and the
    launch flag judged together with that policy.  A gate whose state is
    unknown stays silent, and so does everything once the user is polling.
    """

    __slots__ = ("_gates", "_polling")

    _gates: tuple[ChannelGate, ...]
    _polling: Callable[[], bool]

    def __new__(
        cls,
        gates: tuple[ChannelGate, ...],
        polling: Callable[[], bool] | None = None,
    ) -> Self:
        """Check *gates*, in order, unless *polling* says the user polls."""
        self = super().__new__(cls)
        self._gates = gates
        self._polling = (lambda: False) if polling is None else polling
        return self

    @classmethod
    def for_machine(
        cls,
        env: Mapping[str, str] | None = None,
        platform: str | None = None,
    ) -> Self:
        """Return the warning for this process's environment and operating system."""
        env = os.environ if env is None else env
        policy = ManagedPolicy.for_machine(
            sys.platform if platform is None else platform
        )
        return cls(
            (
                *EnvGate.opt_outs(env),
                EnvGate.provider(env),
                ManagedPolicyGate(policy),
                LaunchFlagGate(env, policy),
            ),
            PollSetting.here().is_on,
        )

    @classmethod
    def add_to(cls, output: dict[str, object], source: object) -> dict[str, object]:
        """Return SessionStart *output* with this machine's warning added."""
        return cls.for_machine().apply(output, source=source)

    def apply(self, output: dict[str, object], *, source: object) -> dict[str, object]:
        """Return *output* carrying the warning to the model and, at startup, the user.

        The model reads it in ``additionalContext`` on every SessionStart, so
        it survives a resume, a clear and a compaction.  The user sees it in
        ``systemMessage`` only when *source* is ``"startup"``.  *output* is
        returned unchanged when there is nothing to say.
        """
        text = self.text()
        if text is None:
            return output
        specific = cast("dict[str, object]", output["hookSpecificOutput"])
        context = f"{specific['additionalContext']} {text}"
        result = {
            **output,
            "hookSpecificOutput": {**specific, "additionalContext": context},
        }
        user_text = self.text(user=True) if source == "startup" else None
        if user_text is not None:
            result["systemMessage"] = user_text
        return result

    def text(self, *, user: bool = False) -> str | None:
        """Return the warning, or ``None`` when there is nothing to say.

        With *user*, gates the user cannot act on are left out.  A gate that
        raises is logged and skipped: this runs while a session starts and
        must neither break it nor hide what the other gates found.
        """
        closed = [g for g in self._closed() if g.notify_user or not user]
        if not closed:
            return None
        reasons = "; ".join(gate.reason for gate in closed)
        fixes = " ".join(gate.fix for gate in closed if gate.fix)
        return (
            f"biff: channels are not live in this session ({reasons}). "
            "New messages will not arrive on their own. "
            f"Run /biff:read 2m to poll. {fixes}"
        ).rstrip()

    def _closed(self) -> list[ClosedGate]:
        """Return what every gate reports; empty while the user polls."""
        if self._polling():
            return []
        closed: list[ClosedGate] = []
        for gate in self._gates:
            try:
                closed.extend(gate.closed())
            except Exception:
                logger.warning(
                    "channel gate %s failed", type(gate).__name__, exc_info=True
                )
        return closed
