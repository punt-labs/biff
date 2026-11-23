"""A filesystem watch that tells the server when the local relay has an arrival."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import shutil
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, ClassVar, Self

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.utils import UnsupportedLibcError

from biff.directory_kqueue import DirectoryKqueue
from biff.failure_log import FailureLog

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver

logger = logging.getLogger(__name__)

_THREAD_HOOK_INSTALLED: list[bool] = [False]


def _reset_dead_inotify_reader() -> None:
    """On Linux, drop watchdog's shared inotify reader if its thread has died.

    watchdog keeps one process-wide reader thread (``InotifyFD._instance``); if
    it dies, every emitter stays alive and a rebuild would reuse the corpse.
    This relies on watchdog's private names, so the dependency is pinned to the
    major version they were read from, and a test fails if they go away.
    """
    try:
        module = importlib.import_module("watchdog.observers.inotify_c")
        reader_class: type[object] = module.__dict__["InotifyFD"]
        shared = "_instance"
        lock: threading.Lock = vars(reader_class)["_global_lock"]
        with lock:
            reader: threading.Thread | None = vars(reader_class).get(shared)
            if reader is not None and not reader.is_alive():
                with suppress(OSError):
                    vars(reader_class)["close"](reader)
                setattr(reader_class, shared, None)
                logger.warning("Replaced a dead inotify reader thread")
    except (ImportError, KeyError, OSError, UnsupportedLibcError):
        logger.debug("No inotify reader to inspect on this platform")


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
                exc_info=args.exc_value,
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


class LocalWatch:
    """Call a function on the running loop whenever an arrival lands.

    Watches the relay's data directory, not its files, because
    ``atomic_write`` replaces a file's inode.  The OS reports each change, so
    no timer is involved (DES-087).  :meth:`start` and :meth:`stop` bracket the
    watch, :meth:`close` ends it for good (a later :meth:`start` does nothing,
    so a start that finishes after a cancelled poller's cleanup tears nothing up
    twice and leaks nothing), and the instance is also a context manager.

    Two watchers run on macOS: the OS file-event observer (FSEvents), which
    sees everything but reports another process's writes in batches seconds
    late (up to ~9s measured), and :class:`DirectoryKqueue`, which is prompt.
    Either waking the signal is enough and a second wake is free.  Descriptor
    use is constant in the number of files.

    :meth:`unhealthy_reason` runs on the poller's liveness pass and names the
    first thing wrong: a dead observer, emitter or kqueue thread, a directory
    that was replaced (``st_dev``/``st_ino``), or a canary that the observer
    did not report, which also catches an observer that is alive and deaf (a
    stalled FSEvents stream, a dead process-wide inotify reader).

    The canary lives in a private subdirectory of the watched directory that
    only this watch observes (a second path on the same observer).  Writing to
    the shared directory itself would wake every other session watching it, one
    full pass each, every check: a cadence across sessions that DES-087 forbids.
    The subdirectory is created and removed once, at start and stop.  On macOS
    the canary is reported by a kqueue on that subdirectory (FSEvents may lag it
    by seconds, so it is not what proves delivery there); on Linux, by inotify.
    """

    JOIN_SECONDS: ClassVar[float] = DirectoryKqueue.JOIN_SECONDS
    CANARY_SECONDS: ClassVar[float] = 10.0
    CANARY_PREFIX: ClassVar[str] = ".biff-canary-"

    __slots__ = (
        "_canary",
        "_canary_dir",
        "_canary_kqueue",
        "_closed",
        "_data_dir",
        "_identity",
        "_kqueue",
        "_lock",
        "_loop",
        "_observer",
        "_on_change",
        "_stuck_kqueue",
    )

    _data_dir: Path
    _on_change: Callable[[], None]
    _loop: asyncio.AbstractEventLoop
    _identity: tuple[int, int]
    _observer: BaseObserver | None
    _kqueue: DirectoryKqueue | None
    _canary_kqueue: DirectoryKqueue | None
    _stuck_kqueue: DirectoryKqueue | None
    _canary: threading.Event
    _canary_dir: Path
    _lock: threading.RLock
    _closed: bool

    def __new__(cls, data_dir: Path, on_change: Callable[[], None]) -> Self:
        """Create a stopped watch on *data_dir*; call it from the event loop."""
        self = super().__new__(cls)
        self._data_dir = data_dir
        self._on_change = on_change
        self._loop = asyncio.get_running_loop()
        self._identity = (-1, -1)
        self._observer = None
        self._kqueue = None
        self._canary_kqueue = None
        self._stuck_kqueue = None
        self._canary = threading.Event()
        self._canary_dir = data_dir / f"{self.CANARY_PREFIX}{os.getpid()}-{id(self):x}"
        self._lock = threading.RLock()
        self._closed = False
        return self

    @property
    def data_dir(self) -> Path:
        """The directory being watched."""
        return self._data_dir

    def start(self) -> None:
        """Start watching, creating the directory if it is missing.

        Does nothing once :meth:`close` has run.  A failure stops and joins
        whatever had already started, then raises the cause.
        """
        with self._lock:
            if self._closed:
                return
            _log_thread_failures()
            observer: BaseObserver | None = None
            try:
                self._data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                info = self._data_dir.stat()
                self._identity = (info.st_dev, info.st_ino)
                self._canary_dir.mkdir(mode=0o700, exist_ok=True)
                observer = Observer()
                for path, loop, notify in (
                    (self._data_dir, self._loop, self._on_change),
                    (self._canary_dir, None, self._canary.set),
                ):
                    handler = ArrivalHandler(loop, notify)
                    observer.schedule(handler, str(path), recursive=False)
                observer.start()
                self._observer = observer
                if sys.platform == "darwin":
                    self._start_kqueue()
            except BaseException:
                self._teardown(observer)
                raise

    def _start_kqueue(self) -> None:
        stuck = self._stuck_kqueue
        if stuck is not None and stuck.is_alive:
            logger.warning("Not starting a second kqueue over one that will not stop")
            return
        self._stuck_kqueue = None
        self._kqueue = DirectoryKqueue(self._data_dir, self._wake)
        # The canary is proven by this prompt path on macOS; FSEvents may lag it.
        self._canary_kqueue = DirectoryKqueue(self._canary_dir, self._canary.set)

    def _wake(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._on_change)
        except RuntimeError:
            logger.debug("Local relay watch fired after the event loop closed")

    def unhealthy_reason(self, canary_timeout: float | None = None) -> str | None:
        """Return what is wrong with the watch, or ``None`` if it is healthy.

        Blocks for up to *canary_timeout* seconds while it proves an event can
        still arrive end to end; call it on a worker thread.
        """
        reason = self._thread_reason(self._observer)
        if reason is None:
            reason = self._directory_reason()
        if reason is None:
            reason = self._canary_reason(canary_timeout or self.CANARY_SECONDS)
        return reason

    def _thread_reason(self, observer: BaseObserver | None) -> str | None:
        if observer is None:
            return "watch not started"
        if not observer.is_alive():
            return "observer thread dead"
        if not observer.emitters or not all(e.is_alive() for e in observer.emitters):
            return "emitter thread dead"
        kqueues = (self._kqueue, self._canary_kqueue)
        if sys.platform == "darwin" and not all(k and k.is_alive for k in kqueues):
            return "kqueue thread dead"
        return None

    def _directory_reason(self) -> str | None:
        try:
            info = self._data_dir.stat()
        except OSError as exc:
            return f"directory missing ({exc.strerror})"
        if (info.st_dev, info.st_ino) != self._identity:
            return "directory replaced (device or inode changed)"
        return None

    def _canary_reason(self, timeout: float) -> str | None:
        ping = self._canary_dir / "userinbox-canary.jsonl"
        self._canary.clear()
        try:
            ping.write_text("")
        except OSError as exc:
            return f"canary entry could not be written ({exc.strerror})"
        seen = self._canary.wait(timeout)
        ping.unlink(missing_ok=True)
        return None if seen else f"canary event not seen within {timeout:g}s"

    def stop(self) -> None:
        """Stop watching (it can be started again)."""
        with self._lock:
            self._teardown(None)

    def close(self) -> None:
        """Stop watching for good; a later :meth:`start` does nothing."""
        with self._lock:
            self._closed = True
            self._teardown(None)

    def _teardown(self, started: BaseObserver | None) -> None:
        observer, self._observer = self._observer, None
        kqueue, self._kqueue = self._kqueue, None
        canary_kqueue, self._canary_kqueue = self._canary_kqueue, None
        for obs in (observer, started):
            if obs is not None and obs.ident is not None:
                obs.stop()
                obs.join(timeout=self.JOIN_SECONDS)
                if obs.is_alive():
                    logger.warning("Local relay watch observer did not stop")
        if kqueue is not None and not kqueue.stop():
            logger.warning("Local relay kqueue thread did not stop")
            self._stuck_kqueue = kqueue
        if canary_kqueue is not None:
            canary_kqueue.stop()
        shutil.rmtree(self._canary_dir, ignore_errors=True)
        _reset_dead_inotify_reader()

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
        """Stop watching for good."""
        self.close()


class WatchKeeper:
    """Keep a set of watches running, rebuilding any that has gone bad.

    :meth:`start` and :meth:`check` block (they start and join threads and wait
    for a canary event); call them with ``asyncio.to_thread`` (:meth:`acheck`).
    A failure to start, or a failed check, goes to a
    :class:`~biff.failure_log.FailureLog` (onset, quiet retries, a reminder
    every ``REMINDER_SECONDS``: arrivals are then only picked up by the
    liveness pass).  Recovery is logged once, and only after a later check has
    proven the watch (canary included), never merely because a rebuild did not
    raise.
    """

    REMINDER_SECONDS: ClassVar[float] = 600.0

    __slots__ = ("_log", "_watches")

    _watches: tuple[LocalWatch, ...]
    _log: FailureLog

    def __new__(cls, watches: tuple[LocalWatch, ...]) -> Self:
        """Create a keeper over *watches*, none of them started yet."""
        self = super().__new__(cls)
        self._watches = watches
        self._log = FailureLog(logger, remind_every=cls.REMINDER_SECONDS)
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
            what = f"Local relay watch on {watch.data_dir}"
            reason = watch.unhealthy_reason()
            if reason is None:
                self._log.recovered(what)
                continue
            self._log.failed(what, RuntimeError(reason))
            self._build(watch)
            rebuilt = True
        return rebuilt

    async def acheck(self) -> bool:
        """Run :meth:`check` on a worker thread; ``False`` with nothing to watch."""
        return bool(self._watches) and await asyncio.to_thread(self.check)

    def stop(self) -> None:
        """Stop every watch for good."""
        for watch in self._watches:
            watch.close()

    def _build(self, watch: LocalWatch) -> None:
        try:
            watch.stop()
            watch.start()
        except (OSError, RuntimeError, ValueError) as exc:
            self._log.failed(f"Local relay watch on {watch.data_dir}", exc)
