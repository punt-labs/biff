"""The command that starts polling, for the plugin that is loaded."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Self, cast, final

__all__ = ["PollCommand"]

logger = logging.getLogger(__name__)


@final
class PollCommand:
    """The slash command that starts polling every two minutes.

    The production plugin's command is ``/biff:read``; the ``--plugin-dir``
    development plugin (``biff-dev``) names its own ``/biff-dev:read-dev``.
    Claude Code gives a plugin's hooks ``CLAUDE_PLUGIN_ROOT``, and the plugin's
    own manifest, ``.claude-plugin/plugin.json``, names which plugin it is.
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
        name = cls._manifest_name(env.get("CLAUDE_PLUGIN_ROOT", ""))
        if name.endswith("-dev"):
            return cls("/biff-dev:read-dev 2m")
        if name != "biff":
            logger.info("plugin name %r is not biff or biff-dev; assuming biff", name)
        return cls("/biff:read 2m")

    @staticmethod
    def _manifest_name(plugin_root: str) -> str:
        """Return the top-level ``name`` of the plugin manifest, or ``""``."""
        if not plugin_root:
            logger.info("CLAUDE_PLUGIN_ROOT is not set; cannot read plugin.json")
            return ""
        path = Path(plugin_root) / ".claude-plugin" / "plugin.json"
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.info("plugin.json unreadable: %s", path, exc_info=True)
            return ""
        name = (
            cast("dict[str, object]", manifest).get("name")
            if isinstance(manifest, dict)
            else None
        )
        if not isinstance(name, str):
            logger.info("plugin.json has no top-level name: %s", path)
            return ""
        return name

    @property
    def text(self) -> str:
        """The command with its interval."""
        return self._text
