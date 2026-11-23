"""Age-out of talk invites that nobody answered."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from biff.talk_types import PendingInvite, QueuedNotification


class InviteExpiry:
    """Reap invites whose monotonic age has reached a time-to-live.

    notification.tex ``ExpirePendingInvite``: an invite whose inviter never
    returns and never withdraws would otherwise strand the ``[TALK]`` marker.
    Only invites age out: a queued message clears by being drained (talk.tex
    ``DrainMessage``), never by time-to-live.
    """

    __slots__ = ("_ttl",)

    _ttl: float

    def __new__(cls, ttl: float) -> Self:
        """Create an expiry that reaps invites *ttl* seconds old."""
        self = super().__new__(cls)
        self._ttl = ttl
        return self

    @property
    def ttl(self) -> float:
        """Seconds an invite lives."""
        return self._ttl

    def pending(
        self, invites: Mapping[str, PendingInvite], clock: float
    ) -> dict[str, PendingInvite]:
        """Return the drained invites younger than the time-to-live at *clock*."""
        return {
            user: invite
            for user, invite in invites.items()
            if clock - invite.arrived < self._ttl
        }

    def queued(
        self, queue: Iterable[QueuedNotification], clock: float
    ) -> deque[QueuedNotification]:
        """Return the undrained frames left once stale invites are dropped."""
        return deque(
            q
            for q in queue
            if not (q.notif.is_invite and clock - q.arrived >= self._ttl)
        )
