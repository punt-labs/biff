"""What doctor's channel check uses of a relay to find the sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from biff.models import UserSession

__all__ = ["CheckRelay"]


class CheckRelay(Protocol):
    """What the check uses of a relay to find the sessions; ``NatsRelay`` has it."""

    async def list_sessions_for_user(self, user: str) -> list[UserSession]:
        """Return the sessions *user* has in this relay's repo.

        Raises when the lookup fails, so a failure never reads as "none running".
        """
        ...

    async def close(self) -> None:
        """Close the connection."""
        ...
