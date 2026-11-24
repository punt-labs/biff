"""A kqueue on a directory descriptor, for macOS and BSD."""

from __future__ import annotations

import logging
import os
import select
import sys
import threading
from contextlib import suppress
from typing import TYPE_CHECKING, ClassVar, Self

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)


if sys.platform == "darwin":

    class DirectoryKqueue:
        """A kqueue on the directory descriptor alone (macOS and BSD).

        It reports every entry added, removed or renamed in the directory at once
        and costs two descriptors however many files the directory holds.  It does
        not see an append to a file already there; the OS file-event observer does,
        and the relay writes mailboxes by rename so the append case does not arise.
        The thread ends when the directory itself is removed or renamed
        (:attr:`is_alive` is then false), and :meth:`stop` ends it otherwise.
        """

        JOIN_SECONDS: ClassVar[float] = 2.0

        __slots__ = ("_fds", "_kq", "_thread")

        _fds: tuple[int, int, int]
        _kq: select.kqueue
        _thread: threading.Thread

        def __new__(cls, path: Path, notify: Callable[[], None]) -> Self:
            """Start watching *path*, calling *notify* from the watch thread.

            Each resource is opened inside the ``try``, so a failure closes exactly
            what was opened and nothing leaks.
            """
            self = super().__new__(cls)
            opened: list[int] = []
            queue: select.kqueue | None = None
            try:
                directory = os.open(path, os.O_RDONLY)
                opened.append(directory)
                read_end, write_end = os.pipe()
                opened += [read_end, write_end]
                queue = select.kqueue()
                vnode = (
                    select.KQ_NOTE_WRITE
                    | select.KQ_NOTE_DELETE
                    | select.KQ_NOTE_RENAME
                    | select.KQ_NOTE_REVOKE
                )
                queue.control(
                    [
                        select.kevent(
                            directory,
                            select.KQ_FILTER_VNODE,
                            select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                            vnode,
                        ),
                        select.kevent(
                            read_end, select.KQ_FILTER_READ, select.KQ_EV_ADD
                        ),
                    ],
                    0,
                    0,
                )
                self._fds = (directory, read_end, write_end)
                self._kq = queue
                self._thread = threading.Thread(
                    target=self._run,
                    args=(notify,),
                    name="biff-watch-kqueue",
                    daemon=True,
                )
                self._thread.start()
            except BaseException:
                for fd in opened:
                    with suppress(OSError):
                        os.close(fd)
                if queue is not None:
                    queue.close()
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
                self._thread.join(timeout=self.JOIN_SECONDS)
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

else:

    class DirectoryKqueue:
        """The same interface where there is no kqueue; it cannot be started."""

        JOIN_SECONDS: ClassVar[float] = 2.0

        def __new__(cls, path: Path, notify: Callable[[], None]) -> Self:
            """Refuse: ``select.kqueue`` exists only on macOS and BSD."""
            msg = "kqueue is not available on this platform"
            raise NotImplementedError(msg)

        @property
        def is_alive(self) -> bool:
            """Never alive: it cannot be started."""
            return False

        def stop(self) -> bool:
            """Nothing to stop."""
            return True
