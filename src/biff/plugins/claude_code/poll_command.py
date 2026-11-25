"""The command that starts polling, for the plugin that is loaded."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Self, final

from biff.plugins.claude_code.plugin_root import PluginRoot

__all__ = ["PollCommand"]

logger = logging.getLogger(__name__)


@final
class PollCommand:
    """The slash command that starts polling every two minutes.

    The production plugin's command is ``/biff:read``; the ``--plugin-dir``
    development plugin (``biff-dev``) names its own ``/biff-dev:read-dev``.
    :class:`PluginRoot` reads which plugin the hooks were loaded from.
    """

    __slots__ = ("_text",)

    _text: str

    def __new__(cls, text: str) -> Self:
        """Wrap the command *text*."""
        self = super().__new__(cls)
        self._text = text
        return self

    @classmethod
    def for_env(cls, env: Mapping[str, str]) -> Self:
        """Return the command for the plugin whose root *env* names.

        A missing or unusable manifest is logged at INFO and means the
        production plugin.
        """
        name = PluginRoot(env).name
        if name.endswith("-dev"):
            return cls("/biff-dev:read-dev 2m")
        if name != "biff":
            logger.info("plugin name %r is not biff or biff-dev; assuming biff", name)
        return cls("/biff:read 2m")

    @property
    def text(self) -> str:
        """The command with its interval."""
        return self._text
