"""The filesystem event handler that turns a relay-directory change into a wake."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self

from watchdog.events import FileSystemEventHandler

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

    from watchdog.events import FileSystemEvent

logger = logging.getLogger("biff.local_watch")


class ArrivalHandler(FileSystemEventHandler):
    """Forward each change to an arrival file onto the event loop.

    An arrival file is one of the two mailboxes (``inbox-<user>-<tty>.jsonl``
    and the bare-user ``userinbox-<user>.jsonl``), ``sessions.json`` or
    ``wall.json``.  Temp files (``atomic_write``'s ``*.tmp``) and the lock
    file are not arrivals.

    The directory's own modification (an entry added, removed or renamed) also
    wakes the loop.  An atomic write is a temp file renamed over its target,
    and kqueue sometimes reports the temp file's creation and removal but
    never the rename; the directory event still arrives, so it is the one
    signal that survives that gap.  Reads and the lock file's ``flock`` change
    no entry, so they wake nothing.
    """

    _FILES: ClassVar[frozenset[str]] = frozenset({"sessions.json", "wall.json"})
    _MAILBOX_PREFIXES: ClassVar[tuple[str, ...]] = ("inbox-", "userinbox-")
    # Reads open and close files too; only a change to the data counts, or the
    # recompute that reads an inbox would wake itself again.
    _CHANGES: ClassVar[frozenset[str]] = frozenset(
        {"created", "modified", "moved", "deleted"}
    )

    _loop: asyncio.AbstractEventLoop | None
    _on_change: Callable[[], None]

    def __new__(
        cls, loop: asyncio.AbstractEventLoop | None, on_change: Callable[[], None]
    ) -> Self:
        """Create a handler that calls *on_change* for each arrival.

        It runs on *loop*, or on the watcher's own thread if *loop* is ``None``
        (the canary, which a thread blocked on it must be able to see).
        """
        self = super().__new__(cls)
        self._loop = loop
        self._on_change = on_change
        return self

    @classmethod
    def is_arrival_file(cls, name: str) -> bool:
        """Whether *name* is a local-relay file whose change is an arrival."""
        if name in cls._FILES:
            return True
        return name.endswith(".jsonl") and name.startswith(cls._MAILBOX_PREFIXES)

    def is_arrival(self, event: FileSystemEvent) -> bool:
        """Whether *event* is an arrival: a directory entry change, or a mailbox."""
        if event.is_directory:
            return event.event_type == "modified"
        # An atomic replace is reported as a move: the new name is dest_path.
        names = (os.fsdecode(event.src_path), os.fsdecode(event.dest_path))
        return event.event_type in self._CHANGES and any(
            self.is_arrival_file(Path(n).name) for n in names if n
        )

    def on_any_event(self, event: FileSystemEvent) -> None:
        """Wake the loop when *event* is an arrival."""
        if not self.is_arrival(event):
            return
        try:
            if self._loop is None:
                self._on_change()
            else:
                self._loop.call_soon_threadsafe(self._on_change)
        except RuntimeError:
            logger.debug("Local relay watch fired after the event loop closed")
