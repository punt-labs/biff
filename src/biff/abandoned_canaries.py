"""The private directory of watch probes, and reclaiming those of exited sessions."""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_PROBE_NAME = re.compile(r"(\d+)-[0-9a-f]+")


class AbandonedCanaries:
    """Probe directories named ``<pid>-<id>`` under one private root.

    A signal ends a server without running the watch's cleanup, so its probe
    directory stays behind.  The root is only trusted when it is a real
    directory owned by this user and closed to everyone else, and nothing
    under it is followed through a link.  A running session's probe is never
    touched.
    """

    @staticmethod
    def secure(root: Path) -> bool:
        """Create *root* (mode 0700) or accept it only if it is private and ours."""
        try:
            root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError:
            return False
        try:
            info = root.lstat()
        except OSError:
            return False
        return (
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700
        )

    @staticmethod
    def reclaim(root: Path) -> None:
        """Remove every probe directory in *root* whose process is gone."""
        for entry in root.iterdir():
            try:
                AbandonedCanaries._reap(entry)
            except FileNotFoundError:
                continue  # another session removed it first
            except OSError as exc:
                logger.info("Skipping watch probe %s: %s", entry.name, exc)

    @staticmethod
    def _reap(entry: Path) -> None:
        found = _PROBE_NAME.fullmatch(entry.name)
        is_dir = stat.S_ISDIR(entry.lstat().st_mode)  # a link is not one
        if found and is_dir and not AbandonedCanaries._is_running(int(found[1])):
            shutil.rmtree(entry, ignore_errors=True)

    @staticmethod
    def _is_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # it exists, owned by someone else
        return True
