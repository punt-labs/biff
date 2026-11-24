"""Reclaim the probe directories that exited sessions left in a watched directory."""

from __future__ import annotations

import os
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class AbandonedCanaries:
    """Probe directories named ``<pid>-<id>`` whose process has exited.

    A signal ends a server without running the watch's cleanup, so its probe
    directory stays behind.  A running session's probe is never touched.
    """

    @staticmethod
    def reclaim(root: Path) -> None:
        """Remove every probe directory in *root* whose process is gone."""
        for entry in root.iterdir():
            pid = entry.name.partition("-")[0]
            if pid.isdigit() and not AbandonedCanaries._is_running(int(pid)):
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
