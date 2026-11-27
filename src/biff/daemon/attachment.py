"""One connection's hold on a session."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

    from biff.daemon.sessions import Session

__all__ = ["Attachment"]


def _ignore() -> None:
    """Do nothing: the default reaction to being replaced."""


@final
class Attachment:
    """A connection bound to one session for the length of the connection."""

    __slots__ = ("_on_ended", "_on_replaced", "_run", "_session", "_subscription")

    _session: Session
    _run: str
    _subscription: asyncio.Task[None] | None
    _on_replaced: Callable[[], None]
    _on_ended: Callable[[], None]

    def __new__(cls, session: Session, run: str) -> Self:
        """Attach to *session* as the run *run* ("" when the client claims none)."""
        self = super().__new__(cls)
        self._session = session
        self._run = run
        self._subscription = None
        self._on_replaced = _ignore
        self._on_ended = _ignore
        return self

    @property
    def session(self) -> Session:
        """The session this connection is bound to, and no other."""
        return self._session

    @property
    def is_current(self) -> bool:
        """Whether this connection's run is still the session's run."""
        return self._run in ("", self._session.run)

    def on_replaced(self, callback: Callable[[], None]) -> None:
        """Call *callback* when a newer run replaces this connection's run."""
        self._on_replaced = callback

    def on_ended(self, callback: Callable[[], None]) -> None:
        """Call *callback* when the session this connection is bound to ends."""
        self._on_ended = callback

    def replaced_by(self, run: str) -> bool:
        """Whether a hello for *run* replaces the run this connection claims."""
        return self._run not in ("", run)

    def replace(self) -> None:
        """Release the subscription and tell the owner this run is over."""
        self.release_subscription()
        self._on_replaced()

    def end(self) -> None:
        """Release the subscription and tell the owner the session is over."""
        self.release_subscription()
        self._on_ended()

    def hold_subscription(self, task: asyncio.Task[None]) -> bool:
        """Keep *task* as this connection's subscription; refuse a second one."""
        if self._subscription is not None:
            return False
        self._subscription = task
        return True

    def release_subscription(self) -> None:
        """Stop this connection's subscription, if it has one."""
        if self._subscription is not None:
            self._subscription.cancel()
            self._subscription = None

    def detach(self) -> None:
        """Release the connection's hold; the session stays, dormant if alone."""
        self.release_subscription()
        self._session.detach(self)
