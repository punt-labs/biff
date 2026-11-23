"""Add the channel warning to SessionStart output without ever losing the output."""

from __future__ import annotations

import importlib
import logging
from typing import Protocol, cast, final

__all__ = ["SessionOutput"]

logger = logging.getLogger(__name__)

_WARNING_MODULE = "biff.plugins.claude_code.startup_warning"


class _Warner(Protocol):
    @classmethod
    def add_to(cls, output: dict[str, object], source: object) -> dict[str, object]:
        """Return *output* with the warning added."""
        ...


@final
class SessionOutput:
    """SessionStart hook output, which a failing warning must never cost."""

    @staticmethod
    def warned(output: dict[str, object], source: object) -> dict[str, object]:
        """Return *output* with the channel warning, or *output* itself on any failure.

        The warning module is imported here, inside the guard, so a fault in
        any gate, in the policy files, or in the import itself is logged at
        WARNING and biff's own session context is still emitted.  *source* is
        the SessionStart payload's ``source``.
        """
        try:
            module = importlib.import_module(_WARNING_MODULE)
            warner = cast("type[_Warner]", module.StartupWarning)
            return warner.add_to(output, source)
        except Exception:
            logger.warning("channel warning failed at SessionStart", exc_info=True)
            return output
