"""How ``claude`` was started, and how to restart it so biff is a channel."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, Self, final

from biff.plugins.claude_code.claude_argv import (
    BIFF_CHANNEL,
    CHANNELS_FLAG,
    DEV_FLAG,
    ClaudeArgv,
)

__all__ = ["ClaudeLaunch"]

logger = logging.getLogger(__name__)

_PLUGIN_DIR_FLAG: Final = "--plugin-dir"
_RESTART: Final = (
    f"Restart Claude Code with {DEV_FLAG} {BIFF_CHANNEL}. "
    f"Organizations that list biff in allowedChannelPlugins can use "
    f"{CHANNELS_FLAG} {BIFF_CHANNEL} instead."
)


@final
class ClaudeLaunch:
    """One ``claude`` command line, parsed, plus the plugin it loaded biff from."""

    __slots__ = ("_argv", "_env", "_raw")

    _argv: ClaudeArgv
    _env: Mapping[str, str]
    _raw: Sequence[str]

    def __new__(cls, raw: Sequence[str], env: Mapping[str, str]) -> Self:
        """Parse *raw* (program first); *env* carries ``CLAUDE_PLUGIN_ROOT``."""
        self = super().__new__(cls)
        self._raw = raw
        self._argv = ClaudeArgv(raw)
        self._env = env
        return self

    @property
    def argv(self) -> ClaudeArgv:
        """The channel-related flags of this command line."""
        return self._argv

    def restart_advice(self) -> str:
        """Name the flag that would register biff on the next start.

        A ``--plugin-dir`` plugin belongs to the ``inline`` marketplace, so
        ``@punt-labs`` matches nothing there and no ``allowedChannelPlugins``
        entry can name it: only the development flag with the inline entry works.
        """
        if not any(a.partition("=")[0] == _PLUGIN_DIR_FLAG for a in self._raw):
            return _RESTART
        return f"Restart Claude Code with {DEV_FLAG} plugin:{self._name()}@inline."

    def _name(self) -> str:
        """Return the running plugin's manifest name; ``biff-dev`` when unreadable."""
        root = Path(self._env.get("CLAUDE_PLUGIN_ROOT", ""))
        try:
            manifest = json.loads((root / ".claude-plugin/plugin.json").read_text())
            return str(manifest["name"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.info("plugin manifest unreadable: %s", type(exc).__name__)
            return "biff-dev"
