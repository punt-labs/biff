"""Add the channel warning to SessionStart output without ever losing the output."""

from __future__ import annotations

import importlib
import logging
import os
from typing import Protocol, cast, final

from biff.plugins.claude_code.poll_command import PollCommand

__all__ = ["SessionOutput"]

logger = logging.getLogger(__name__)

_WARNING_MODULE = "biff.plugins.claude_code.startup_warning"


class _Warner(Protocol):
    @classmethod
    def add_to(
        cls, output: dict[str, object], data: dict[str, object]
    ) -> dict[str, object]:
        """Return *output* with the warning added."""
        ...


@final
class SessionOutput:
    """SessionStart hook output, which a failing warning must never cost."""

    @staticmethod
    def warned(output: dict[str, object], data: dict[str, object]) -> dict[str, object]:
        """Return *output* with the channel warning for the payload *data*.

        The warning module is imported here, inside the guard, so a fault in
        any part of the warning, or in the import itself, is logged at WARNING
        and biff's own session context is still emitted.  The log is not
        shown to anyone, so the model is also told the check failed.
        """
        try:
            module = importlib.import_module(_WARNING_MODULE)
            warner = cast("type[_Warner]", module.StartupWarning)
            return warner.add_to(output, data)
        except Exception:
            logger.warning("channel warning failed at SessionStart", exc_info=True)
            specific = cast("dict[str, object]", output["hookSpecificOutput"])
            command = PollCommand.for_env(os.environ).text
            context = (
                f"{specific['additionalContext']} biff could not check whether "
                f"channels are live; if messages do not arrive, run {command}."
            )
            return {
                **output,
                "hookSpecificOutput": {**specific, "additionalContext": context},
            }
