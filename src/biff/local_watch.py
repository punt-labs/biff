"""A filesystem watch that tells the server when the local relay has an arrival."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, ClassVar, Self

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.kqueue import KqueueObserver

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver

logger = logging.getLogger(__name__)


class ArrivalHandler(FileSystemEventHandler):
    """Forward each change to an arrival file onto the event loop.

    An arrival file is one of the two mailboxes (``inbox-<user>-<tty>.jsonl``
    and the bare-user ``userinbox-<user>.jsonl``), ``sessions.json`` or
    ``wall.json``.  Temp files (``atomic_write``'s ``*.tmp``) and the status
    files other components write beside them are not arrivals.
    """

    _FILES: ClassVar[frozenset[str]] = frozenset({"sessions.json", "wall.json"})
    _MAILBOX_PREFIXES: ClassVar[tuple[str, ...]] = ("inbox-", "userinbox-")
    # Reads open and close files too; only a change to the data counts, or the
    # recompute that reads an inbox would wake itself again.
    _CHANGES: ClassVar[frozenset[str]] = frozenset(
        {"created", "modified", "moved", "deleted"}
    )

    _loop: asyncio.AbstractEventLoop
    _on_change: Callable[[], None]

    def __new__(
        cls, loop: asyncio.AbstractEventLoop, on_change: Callable[[], None]
    ) -> Self:
        """Create a handler that calls *on_change* on *loop* for each arrival."""
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

    def on_any_event(self, event: FileSystemEvent) -> None:
        """Wake the loop when *event* changes an arrival file."""
        if event.is_directory or event.event_type not in self._CHANGES:
            return
        # An atomic replace is reported as a move: the new name is dest_path.
        names = (os.fsdecode(event.src_path), os.fsdecode(event.dest_path))
        if not any(self.is_arrival_file(Path(n).name) for n in names if n):
            return
        try:
            self._loop.call_soon_threadsafe(self._on_change)
        except RuntimeError:
            logger.debug("Local relay watch fired after the event loop closed")


class LocalWatch:
    """Call a function on the running loop whenever an arrival lands.

    Watches the relay's data directory, not its files, because
    ``atomic_write`` replaces a file's inode.  The OS reports each change, so
    no timer is involved (DES-087).  Enter it as a context manager from the
    event loop; the watch stops on exit.

    macOS runs two observers.  FSEvents sees every change but reports another
    process's writes in batches seconds late (measured up to ~9s for a wall
    posted by the ``biff`` CLI).  kqueue reports them as they happen but can
    miss a file created and renamed between two of its directory snapshots.
    Either one waking the signal is enough, and a second wake is free, so each
    covers the other's gap.
    """

    __slots__ = ("_data_dir", "_observers", "_on_change")

    _JOIN_SECONDS: ClassVar[float] = 2.0

    _data_dir: Path
    _on_change: Callable[[], None]
    _observers: tuple[BaseObserver, ...]

    def __new__(cls, data_dir: Path, on_change: Callable[[], None]) -> Self:
        """Create a watch on *data_dir* that calls *on_change* on each arrival."""
        self = super().__new__(cls)
        self._data_dir = data_dir
        self._on_change = on_change
        self._observers = ()
        return self

    def __enter__(self) -> Self:
        """Start watching, creating the data directory if it is missing."""
        self._data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        handler = ArrivalHandler(asyncio.get_running_loop(), self._on_change)
        kinds = (KqueueObserver, Observer) if sys.platform == "darwin" else (Observer,)
        observers = tuple(kind() for kind in kinds)
        for observer in observers:
            observer.schedule(handler, str(self._data_dir), recursive=False)
            observer.start()
        self._observers = observers
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop watching."""
        observers, self._observers = self._observers, ()
        for observer in observers:
            observer.stop()
        for observer in observers:
            observer.join(timeout=self._JOIN_SECONDS)
