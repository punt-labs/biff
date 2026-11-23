"""Make two biff plugins in one Claude Code session warn once."""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Self, final

from biff._stdlib import biff_data_dir

__all__ = ["WarningClaim"]

logger = logging.getLogger(__name__)

_UNSAFE: Final = re.compile(r"[^A-Za-z0-9_.-]")
_PRUNE_AFTER: Final = 24 * 60 * 60.0
"""Seconds after which a marker is stale: no session lasts its warning that long."""


@final
class WarningClaim:
    """A once-only claim on the warning for one hook payload.

    The installed ``biff`` plugin and a ``--plugin-dir`` ``biff-dev`` plugin
    both run the SessionStart hook for the same session.  Each tries to create
    one marker file, atomically; only the creator emits the warning.  The key
    is ``CLAUDE_PID``, the payload's ``session_id`` and its ``source``, so a
    compaction of the same session claims afresh.  A payload with no
    ``session_id`` cannot be keyed and always wins.  Markers older than a day
    are removed on each claim.
    """

    __slots__ = ("_directory", "_key")

    _directory: Path
    _key: str | None

    def __new__(
        cls, directory: Path, env: Mapping[str, str], data: Mapping[str, object]
    ) -> Self:
        """Key a claim for the hook payload *data* under *directory*."""
        self = super().__new__(cls)
        self._directory = directory
        session = data.get("session_id")
        if isinstance(session, str) and session:
            pid = env.get("CLAUDE_PID", "")
            parts = (pid if pid.isdecimal() else "0", session, str(data.get("source")))
            self._key = _UNSAFE.sub("_", "-".join(parts))
        else:
            self._key = None
        return self

    @classmethod
    def for_hook(cls, env: Mapping[str, str], data: Mapping[str, object]) -> Self:
        """Return the claim in biff's state directory."""
        return cls(biff_data_dir() / "channel-warnings", env, data)

    def take(self) -> bool:
        """Return whether this caller is the first to claim the warning.

        Fails open: when the marker cannot be written the warning is shown.
        """
        if self._key is None:
            return True
        try:
            self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._prune()
            fd = os.open(
                self._directory / self._key,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            return False
        except OSError:
            logger.info("warning claim unavailable: %s", self._directory, exc_info=True)
            return True
        os.close(fd)
        return True

    def _prune(self) -> None:
        cutoff = time.time() - _PRUNE_AFTER
        for marker in self._directory.iterdir():
            try:
                if marker.stat().st_mtime < cutoff:
                    marker.unlink()
            except OSError:
                logger.info("warning marker not pruned: %s", marker, exc_info=True)
