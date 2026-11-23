"""A filesystem watch that tells the server when the local relay has an arrival."""

from __future__ import annotations

import asyncio
import logging
import os
import select
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, ClassVar, Self

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver

logger = logging.getLogger(__name__)

_THREAD_HOOK_INSTALLED: list[bool] = [False]


def _log_thread_failures() -> None:
    """Send an uncaught exception in a watch thread to the logger, not stderr."""
    if _THREAD_HOOK_INSTALLED[0]:
        return
    _THREAD_HOOK_INSTALLED[0] = True
    previous = threading.excepthook

    def hook(args: threading.ExceptHookArgs) -> None:
        thread = args.thread
        owned = thread is not None and (
            thread.name.startswith("biff-watch")
            or type(thread).__module__.startswith("watchdog")
        )
        if owned and thread is not None:
            logger.error(
                "Local relay watch thread %s died",
                thread.name,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        else:
            previous(args)

    threading.excepthook = hook


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
            self._loop.call_soon_threadsafe(self._on_change)
        except RuntimeError:
            logger.debug("Local relay watch fired after the event loop closed")


class DirectoryKqueue:
    """A kqueue on the directory descriptor alone (macOS and BSD).

    It reports every entry added, removed or renamed in the directory at once
    and costs two descriptors however many files the directory holds.  It does
    not see an append to a file already there; the OS file-event observer does,
    and the relay writes mailboxes by rename so the append case does not arise.
    The thread ends when the directory itself is removed or renamed
    (:attr:`is_alive` is then false), and :meth:`stop` ends it otherwise.
    """

    __slots__ = ("_fds", "_kq", "_thread")

    _fds: tuple[int, int, int]
    _kq: select.kqueue
    _thread: threading.Thread

    def __new__(cls, path: Path, notify: Callable[[], None]) -> Self:
        """Start watching *path*, calling *notify* from the watch thread."""
        self = super().__new__(cls)
        directory = os.open(path, os.O_RDONLY)
        read_end, write_end = os.pipe()
        self._fds = (directory, read_end, write_end)
        self._kq = select.kqueue()
        try:
            vnode = (
                select.KQ_NOTE_WRITE
                | select.KQ_NOTE_DELETE
                | select.KQ_NOTE_RENAME
                | select.KQ_NOTE_REVOKE
            )
            self._kq.control(
                [
                    select.kevent(
                        directory,
                        select.KQ_FILTER_VNODE,
                        select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                        vnode,
                    ),
                    select.kevent(read_end, select.KQ_FILTER_READ, select.KQ_EV_ADD),
                ],
                0,
                0,
            )
            self._thread = threading.Thread(
                target=self._run, args=(notify,), name="biff-watch-kqueue", daemon=True
            )
            self._thread.start()
        except BaseException:
            self._release()
            raise
        return self

    @property
    def is_alive(self) -> bool:
        """Whether the thread is still watching the directory."""
        return self._thread.is_alive()

    def stop(self) -> bool:
        """Stop the thread and free the descriptors; return whether it ended."""
        if self._thread.is_alive():
            os.write(self._fds[2], b"x")
            self._thread.join(timeout=LocalWatch.JOIN_SECONDS)
        ended = not self._thread.is_alive()
        if ended:
            self._release()
        return ended

    def _release(self) -> None:
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                logger.debug("kqueue descriptor %d already closed", fd)
        self._kq.close()

    def _run(self, notify: Callable[[], None]) -> None:
        directory, read_end, _ = self._fds
        gone = select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME | select.KQ_NOTE_REVOKE
        while True:
            for event in self._kq.control(None, 8, None):
                if event.ident == read_end:
                    return
                if event.ident == directory:
                    notify()
                    if event.fflags & gone:
                        return


class LocalWatch:
    """Call a function on the running loop whenever an arrival lands.

    Watches the relay's data directory, not its files, because
    ``atomic_write`` replaces a file's inode.  The OS reports each change, so
    no timer is involved (DES-087).  :meth:`start` and :meth:`stop` bracket the
    watch, and the instance is also a context manager.

    Two watchers run on macOS: the OS file-event observer (FSEvents), which
    sees everything but reports another process's writes in batches seconds
    late (up to ~9s measured), and :class:`DirectoryKqueue`, which is prompt.
    Either waking the signal is enough and a second wake is free.  Descriptor
    use is constant in the number of files.

    :meth:`is_healthy` says whether every watcher is still running and the
    directory is the one that was watched; a directory removed and recreated
    kills the kqueue thread and strands the observer on the old inode, and the
    poller's liveness pass rebuilds the watch.
    """

    JOIN_SECONDS: ClassVar[float] = 2.0

    __slots__ = ("_data_dir", "_inode", "_kqueue", "_loop", "_observer", "_on_change")

    _data_dir: Path
    _on_change: Callable[[], None]
    _loop: asyncio.AbstractEventLoop
    _inode: int
    _observer: BaseObserver | None
    _kqueue: DirectoryKqueue | None

    def __new__(cls, data_dir: Path, on_change: Callable[[], None]) -> Self:
        """Create a stopped watch on *data_dir*; call it from the event loop."""
        self = super().__new__(cls)
        self._data_dir = data_dir
        self._on_change = on_change
        self._loop = asyncio.get_running_loop()
        self._inode = -1
        self._observer = None
        self._kqueue = None
        return self

    @property
    def data_dir(self) -> Path:
        """The directory being watched."""
        return self._data_dir

    def start(self) -> None:
        """Start watching, creating the directory if it is missing.

        A failure stops and joins whatever had already started, then raises.
        """
        _log_thread_failures()
        try:
            self._data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._inode = self._data_dir.stat().st_ino
            handler = ArrivalHandler(self._loop, self._on_change)
            observer = Observer()
            observer.schedule(handler, str(self._data_dir), recursive=False)
            self._observer = observer
            observer.start()
            if sys.platform == "darwin":
                self._kqueue = DirectoryKqueue(self._data_dir, self._wake)
        except BaseException:
            self.stop()
            raise

    def _wake(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._on_change)
        except RuntimeError:
            logger.debug("Local relay watch fired after the event loop closed")

    def is_healthy(self) -> bool:
        """Whether every watcher runs and the directory is the one watched."""
        observer = self._observer
        if observer is None or not observer.is_alive():
            return False
        if not observer.emitters or not all(e.is_alive() for e in observer.emitters):
            return False
        if sys.platform == "darwin" and not (self._kqueue and self._kqueue.is_alive):
            return False
        try:
            return self._data_dir.stat().st_ino == self._inode
        except OSError:
            return False

    def stop(self) -> None:
        """Stop watching; log a warning for any thread that will not end."""
        observer, self._observer = self._observer, None
        kqueue, self._kqueue = self._kqueue, None
        if observer is not None:
            if observer.is_alive():
                observer.stop()
            observer.join(timeout=self.JOIN_SECONDS)
            if observer.is_alive():
                logger.warning("Local relay watch observer did not stop")
        if kqueue is not None and not kqueue.stop():
            logger.warning("Local relay kqueue thread did not stop")

    def __enter__(self) -> Self:
        """Start watching."""
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop watching."""
        self.stop()


class WatchKeeper:
    """Keep a set of watches running, rebuilding any that has gone bad.

    :meth:`start` and :meth:`check` block (they start and join threads); call
    them with ``asyncio.to_thread``.  A watch that fails to start is logged at
    ERROR with its path and exception once, and a recovery is logged once;
    further failed retries stay quiet so the liveness pass does not repeat them.
    """

    __slots__ = ("_failing", "_watches")

    _watches: tuple[LocalWatch, ...]
    _failing: set[Path]

    def __new__(cls, watches: tuple[LocalWatch, ...]) -> Self:
        """Create a keeper over *watches*, none of them started yet."""
        self = super().__new__(cls)
        self._watches = watches
        self._failing = set()
        return self

    @property
    def watching(self) -> bool:
        """Whether there is anything to watch (a push relay has nothing)."""
        return bool(self._watches)

    def start(self) -> None:
        """Start every watch; a failure is logged and retried by :meth:`check`."""
        for watch in self._watches:
            self._build(watch)

    def check(self) -> bool:
        """Rebuild each unhealthy watch; return whether any was rebuilt."""
        rebuilt = False
        for watch in self._watches:
            if watch.is_healthy():
                continue
            path = watch.data_dir
            first = path not in self._failing
            if first:
                logger.warning("Local relay watch on %s is not running", path)
            self._build(watch)
            if first and path not in self._failing:
                logger.warning("Local relay watch on %s is running again", path)
            rebuilt = True
        return rebuilt

    def stop(self) -> None:
        """Stop every watch."""
        for watch in self._watches:
            watch.stop()

    def _build(self, watch: LocalWatch) -> None:
        path = watch.data_dir
        watch.stop()
        try:
            watch.start()
        except Exception:
            if path not in self._failing:
                logger.exception("Local relay watch on %s failed to start", path)
            self._failing.add(path)
        else:
            if path in self._failing:
                self._failing.discard(path)
                logger.warning("Local relay watch on %s is running again", path)
