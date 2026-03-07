"""Readline support for the biff REPL.

Importing and calling ``setup()`` before the first ``input()`` enables:

- **Line editing**: arrow keys, Home/End, Ctrl-A/E, etc.
- **Command history**: up/down arrows recall previous commands.
  Persisted to ``~/.biff/repl_history`` across sessions.
- **Tab completion**: completes command names from ``available_commands()``.
"""

from __future__ import annotations

import atexit
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_HISTORY_PATH = Path.home() / ".biff" / "repl_history"
_MAX_HISTORY = 1000


def setup(command_names: list[str]) -> None:
    """Configure readline for the biff REPL.

    Safe to call even if readline is unavailable (e.g., some minimal
    Python builds).  Failures are logged and silently ignored.
    """
    try:
        import readline  # noqa: PLC0415
    except ImportError:
        logger.debug("readline not available, REPL will lack line editing")
        return

    # Tab completion for command names.
    completions: list[str] = []

    def _completer(text: str, state: int) -> str | None:
        nonlocal completions
        if state == 0:
            completions = [c for c in command_names if c.startswith(text.lower())]
        return completions[state] if state < len(completions) else None

    readline.set_completer(_completer)
    readline.parse_and_bind("tab: complete")

    # macOS uses libedit which needs a different binding syntax.
    # parse_and_bind is idempotent — both forms are harmless.
    readline.parse_and_bind("bind ^I rl_complete")

    # Load persisted history.
    try:
        if _HISTORY_PATH.exists():
            readline.read_history_file(str(_HISTORY_PATH))
    except OSError:
        logger.debug("Failed to load REPL history", exc_info=True)

    # Limit history size.
    readline.set_history_length(_MAX_HISTORY)

    # Save history on exit.
    def _save_history() -> None:
        try:
            _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            readline.write_history_file(str(_HISTORY_PATH))
        except OSError:
            logger.debug("Failed to save REPL history", exc_info=True)

    atexit.register(_save_history)
