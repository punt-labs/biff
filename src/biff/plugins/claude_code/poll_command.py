"""The command that starts polling, for the plugin variant that is loaded."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Self, final

__all__ = ["PollCommand"]


@final
class PollCommand:
    """The slash command that starts polling every two minutes.

    The production plugin's command is ``/biff:read``; the ``--plugin-dir``
    development plugin (``biff-dev``) names its own ``/biff-dev:read-dev``.
    The SessionStart dispatcher passes the loaded plugin's name in
    ``BIFF_PLUGIN_NAME``.
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
        """Return the command for the plugin named in *env*."""
        if env.get("BIFF_PLUGIN_NAME", "").endswith("-dev"):
            return cls("/biff-dev:read-dev 2m")
        return cls("/biff:read 2m")

    @property
    def text(self) -> str:
        """The command with its interval."""
        return self._text
