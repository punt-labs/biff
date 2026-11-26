"""What doctor's channel check uses of a relay and of a check in flight."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from biff.models import UserSession

__all__ = ["CheckPending", "CheckRelay"]


class CheckPending(Protocol):
    """A check in flight: wait for its answer, then close it."""

    async def confirmed(self, timeout: float) -> bool:
        """Whether the session answered within *timeout* seconds."""
        ...

    async def close(self) -> None:
        """Stop listening for the answer."""
        ...


class CheckRelay(Protocol):
    """What the check uses of a relay; ``NatsRelay`` is the one that has it."""

    async def list_sessions_for_user(self, user: str) -> list[UserSession]:
        """Return the sessions *user* has in this relay's repo.

        Raises when the lookup fails, so a failure never reads as "none running".
        """
        ...

    async def request_channel_check(self, session_key: str) -> CheckPending:
        """Send a check to *session_key* and return the wait for its answer."""
        ...

    async def close(self) -> None:
        """Close the connection."""
        ...
