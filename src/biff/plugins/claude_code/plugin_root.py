"""The plugin directory Claude Code loaded biff's hooks from, and its manifest."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Self, cast, final

__all__ = ["PluginRoot"]

_PLUGIN_DIR_FLAG = "--plugin-dir"

logger = logging.getLogger(__name__)


@final
class PluginRoot:
    """``CLAUDE_PLUGIN_ROOT``: biff's own plugin directory, which names the plugin.

    Claude Code gives a plugin's hooks ``CLAUDE_PLUGIN_ROOT``, and the plugin's
    manifest, ``.claude-plugin/plugin.json``, names which plugin it is.
    """

    __slots__ = ("_root",)

    _root: str

    def __new__(cls, env: Mapping[str, str]) -> Self:
        """Read ``CLAUDE_PLUGIN_ROOT`` from *env*."""
        self = super().__new__(cls)
        self._root = env.get("CLAUDE_PLUGIN_ROOT", "")
        return self

    @property
    def name(self) -> str:
        """The manifest's top-level ``name``; ``""`` (logged) when unknown."""
        if not self._root:
            logger.info("CLAUDE_PLUGIN_ROOT is not set; cannot read plugin.json")
            return ""
        path = Path(self._root) / ".claude-plugin" / "plugin.json"
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

    def is_loaded_by(self, argv: Sequence[str]) -> bool:
        """Whether a ``--plugin-dir`` in *argv* (Claude Code's) is this directory."""
        if not self._root:
            return False
        root = Path(self._root).resolve()
        for i, arg in enumerate(argv):
            flag, equals, value = arg.partition("=")
            if flag != _PLUGIN_DIR_FLAG:
                continue
            given = value if equals else (argv[i + 1] if i + 1 < len(argv) else "")
            if given and Path(given).expanduser().resolve() == root:
                return True
        return False
