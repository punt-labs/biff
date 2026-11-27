"""Talk notices: one-line news that a talk frame arrived, never its text."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self, final

from biff.talk_state import MAX_TALK_QUEUE

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

    from pydantic import JsonValue

    from biff.frames import EventKind
    from biff.talk_types import TalkNotification

__all__ = ["TalkNotice", "TalkNotices"]


@final
@dataclass(frozen=True)
class TalkNotice:
    """That a talk frame reached a session: who sent it, and how much waits.

    A line's text is pulled with ``talk_read``, so a notice holds no body.
    """

    _KINDS: ClassVar[Mapping[str, EventKind]] = {
        "invite": "talk_invite",
        "accept": "talk_accept",
        "message": "talk_line",
        "end": "talk_end",
    }

    kind: EventKind
    sender: str
    tty: str
    pending: int

    @classmethod
    def is_noticed(cls, notif: TalkNotification) -> bool:
        """Whether *notif* is news: a withdrawal or a wake poke is not."""
        return notif.ntype in cls._KINDS

    @classmethod
    def of(cls, notif: TalkNotification, *, pending: int) -> Self:
        """Return the notice for *notif*, with *pending* frames now waiting.

        Raises:
            KeyError: *notif* is not noticed (see :meth:`is_noticed`).
        """
        return cls(cls._KINDS[notif.ntype], notif.nfrom, notif.nfrom_tty, pending)

    @property
    def data(self) -> JsonValue:
        """The event's payload."""
        return {"from": self.sender, "tty": self.tty, "pending": self.pending}


@final
class TalkNotices:
    """Hands each notice to every listener of one session.

    A listener that falls behind loses its oldest notices, as the session's
    talk queue does: a flood must not grow memory without bound.
    """

    __slots__ = ("_listeners",)

    _listeners: set[asyncio.Queue[TalkNotice]]

    def __new__(cls) -> Self:
        """Create a hub with no listeners."""
        self = super().__new__(cls)
        self._listeners = set()
        return self

    @contextmanager
    def listen(self) -> Generator[asyncio.Queue[TalkNotice]]:
        """Yield a queue of the notices published while the block runs."""
        queue: asyncio.Queue[TalkNotice] = asyncio.Queue(maxsize=MAX_TALK_QUEUE)
        self._listeners.add(queue)
        try:
            yield queue
        finally:
            self._listeners.discard(queue)

    def publish(self, notice: TalkNotice) -> None:
        """Give *notice* to every listener."""
        for queue in self._listeners:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(notice)
