"""Reclaim the probe directories that exited sessions left in a watched directory."""

from __future__ import annotations

import os
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def reclaim_abandoned_canaries(data_dir: Path, prefix: str) -> None:
    """Remove ``<prefix><pid>-<id>`` directories whose process no longer exists.

    A signal ends a server without running the watch's cleanup, so its probe
    directory stays behind.  A running session's probe is never touched.
    """
    for entry in data_dir.glob(f"{prefix}*"):
        pid = entry.name.removeprefix(prefix).partition("-")[0]
        if pid.isdigit() and not _is_running(int(pid)):
            shutil.rmtree(entry, ignore_errors=True)


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, owned by someone else
    return True
