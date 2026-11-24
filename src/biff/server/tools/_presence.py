"""Where a user is present, and so which repo mailbox they drain."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Self

from biff.models import UserSession
from biff.presence_liveness import live_sessions


@dataclass(frozen=True, slots=True)
class Presence:
    """One user's sessions across the visible repos."""

    _sessions: tuple[UserSession, ...]

    @classmethod
    def of(cls, sessions: Sequence[UserSession], user: str) -> Self:
        """Return *user*'s presence among *sessions*."""
        return cls(tuple(s for s in sessions if s.user == user))

    def __bool__(self) -> bool:
        """Return whether the user has a session in any visible repo."""
        return bool(self._sessions)

    def mailbox_repo(self, local_repo: str) -> str:
        """Pick the repo whose mailbox the user will drain next, or ``""``.

        The mailbox is repo-scoped and a recipient drains only the repo
        where they run, so prefer repos with a live session; with none
        live, any repo they are present in (a dormant session revives and
        drains it).  *local_repo* wins among the candidates; otherwise the
        one with the latest real tool call (``last_tool_at``, not the
        heartbeat), so mail goes where they last worked.  Ties go to the
        first repo by name.  ``""`` means the user is present nowhere.
        """
        live = live_sessions(self._sessions) or self._sessions
        candidates = sorted(live, key=lambda s: s.repo)
        if any(s.repo == local_repo for s in candidates):
            return local_repo
        latest = max(candidates, key=lambda s: s.last_tool_at, default=None)
        return latest.repo if latest else ""
