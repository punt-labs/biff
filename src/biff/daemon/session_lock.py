"""The per-session lock shared by store transitions and the relay work around them."""

from __future__ import annotations

import asyncio
import fcntl
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

_held: ContextVar[frozenset[str]] = ContextVar(
    "cli_session_locks_held", default=frozenset()
)
_POLL_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class SessionLock:
    """The flock every store transition takes, in directory *lock_dir*.

    An attach, an end and a sweep in different processes serialize on one
    session.  A task that already holds a session's lock passes straight
    through, so a store transition made inside :meth:`hold` does not wait on
    its own lock.
    """

    lock_dir: Path

    def is_held(self, session_id: str) -> bool:
        """Whether the current task holds *session_id*'s lock."""
        return session_id in _held.get()

    @contextmanager
    def hold_blocking(self, session_id: str) -> Generator[None]:
        """Hold the lock for one synchronous store transition.

        The lock file lives outside the session directory so create and
        expire, which make and remove that directory, take the same lock as
        every transition.  The OS releases a flock when its process exits, so
        a killed holder can never wedge the session.
        """
        if self.is_held(session_id):
            yield
            return
        self.lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.lock_dir / f"{session_id}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @asynccontextmanager
    async def hold(self, session_id: str) -> AsyncGenerator[None]:
        """Hold the lock across a transition and its relay side effects.

        Waiting polls without blocking the event loop.
        """
        if self.is_held(session_id):
            yield
            return
        self.lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.lock_dir / f"{session_id}.lock").open("a") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(_POLL_SECONDS)
            token = _held.set(_held.get() | {session_id})
            try:
                yield
            finally:
                _held.reset(token)
