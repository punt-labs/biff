"""Wake flags: the push side of a poll that also runs on the clock."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Generator

__all__ = ["Wakes"]


@final
class Wakes:
    """Hands one wake flag to each listener of a session.

    A wake is a flag, not a count: a push that repeats, or arrives while the pass
    it asks for is already due, changes nothing (``PushWake`` in
    ``notification-mail-event.tex``).  A push that is lost is the absence of a
    poke, and the listener's own interval covers it.
    """

    __slots__ = ("_listeners",)

    _listeners: set[asyncio.Event]

    def __new__(cls) -> Self:
        """Create a hub with no listeners."""
        self = super().__new__(cls)
        self._listeners = set()
        return self

    @contextmanager
    def listen(self) -> Generator[asyncio.Event]:
        """Yield a flag that is set by every poke while the block runs."""
        flag = asyncio.Event()
        self._listeners.add(flag)
        try:
            yield flag
        finally:
            self._listeners.discard(flag)

    def poke(self) -> None:
        """Set every listener's flag."""
        for flag in self._listeners:
            flag.set()
