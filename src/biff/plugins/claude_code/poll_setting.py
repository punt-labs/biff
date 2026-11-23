"""Whether the user has already started polling in this repository."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Self, final

from biff._stdlib import find_git_root
from biff.config import load_yaml_local

__all__ = ["PollSetting"]

logger = logging.getLogger(__name__)


@final
class PollSetting:
    """The ``poll_interval`` that ``set_poll_interval`` persists.

    ``/biff:read 2m`` writes a positive ``poll_interval`` to
    ``config.local.yaml``, and ``/biff:read n`` writes ``0``.  An absent key
    is the server's default, not a user who started polling.
    """

    __slots__ = ("_root",)

    _root: Path | None

    def __new__(cls, root: Path | None) -> Self:
        """Read the setting of the repository at *root*, if there is one."""
        self = super().__new__(cls)
        self._root = root
        return self

    @classmethod
    def here(cls) -> Self:
        """Return the setting of the repository this process runs in."""
        return cls(find_git_root())

    def is_on(self) -> bool:
        """Return whether the persisted poll interval is a positive number."""
        if self._root is None:
            return False
        try:
            value = load_yaml_local(self._root).get("poll_interval")
        except OSError:
            logger.info("poll setting unreadable under %s", self._root, exc_info=True)
            return False
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value > 0
        )
