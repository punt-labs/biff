"""Shared local ownership boundary for pinned servers and sentinel reapers."""

from __future__ import annotations

import fcntl
import re
from collections.abc import Generator
from contextlib import contextmanager

from biff._stdlib import biff_data_dir


@contextmanager
def routing_lock(routing_id: str, *, create: bool = False) -> Generator[bool]:
    """Try ownership; only pin-shaped routing keys need exclusion.

    Never unlink these files. Reapers hold the same inode from before reading
    session state until after deletion and sentinel removal, including awaits.
    """
    directory = biff_data_dir() / "session-locks"
    path = directory / f"opencode-{routing_id}.lock"
    # Create even on a reaper's first sighting, before a replacement has ever
    # opened the file. Checking file existence would race that first owner.
    if not create and re.fullmatch(r"[0-9a-f]{16}", routing_id) is None:
        yield True
        return
    directory.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
