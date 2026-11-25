"""How ``claude`` was started, and how to restart it so biff is a channel."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final, Self, final

from biff.plugins.claude_code.claude_argv import (
    BIFF_CHANNEL,
    CHANNELS_FLAG,
    DEV_FLAG,
    ClaudeArgv,
)
from biff.plugins.claude_code.plugin_root import PluginRoot

__all__ = ["ClaudeLaunch"]

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

        A plugin loaded with ``--plugin-dir`` belongs to the ``inline``
        marketplace, so ``@punt-labs`` matches nothing for it and no
        ``allowedChannelPlugins`` entry can name it.  Another plugin's
        ``--plugin-dir`` leaves a marketplace biff where it is.
        """
        root = PluginRoot(self._env)
        if not root.is_loaded_by(self._raw):
            return _RESTART
        name = root.name or "biff-dev"
        return f"Restart Claude Code with {DEV_FLAG} plugin:{name}@inline."
