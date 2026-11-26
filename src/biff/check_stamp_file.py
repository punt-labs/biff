"""Write ``channel_checked_at`` into the per-session unread file (DES-087)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from biff.changed_file import ChangedFile
from biff.check_stamp import CheckStamp
from biff.unread import as_str_dict

if TYPE_CHECKING:
    from pathlib import Path


class CheckStampFile:
    """The one writer of the stamp: the local relay's acknowledgement."""

    @staticmethod
    def record(path: Path, at: float) -> None:
        """Write *at* into the file at *path*, keeping every other field."""
        try:
            data = as_str_dict(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            data = {}
        data[CheckStamp.KEY] = at
        ChangedFile.write(path, json.dumps(data, indent=2) + "\n")
