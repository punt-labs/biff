"""Resolve where a ``write`` is delivered."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from biff._stdlib import display_repo_name
from biff.models import UserSession
from biff.relay import live_sessions
from biff.server.tools._session import WriteTarget, remote_repo, resolve_tty_name
from biff.tty import build_session_key

if TYPE_CHECKING:
    from biff.relay import Relay


@dataclass(frozen=True, slots=True)
class WriteRouter:
    """Routes a write address, sent from *local_repo*, to its delivery target."""

    local_repo: str
    visible_repos: Collection[str]

    async def find(self, relay: Relay, user: str, tty: str | None) -> WriteTarget:
        """Fetch what :meth:`resolve` needs from *relay*, then resolve.

        A named session that has ended leaves no session row, so its repo
        comes from session history: the message then reaches the mailbox
        the sender addressed instead of whichever repo the user is in.
        """
        if not tty:
            return self.resolve([], user, None)
        sessions = await relay.get_sessions_for_repos(frozenset(self.visible_repos))
        if resolve_tty_name(sessions, user, tty, local_repo=self.local_repo):
            return self.resolve(sessions, user, tty)
        addressed_repo = await relay.last_session_repo(f"{user}:{tty}") or ""
        return self.resolve(sessions, user, tty, addressed_repo=addressed_repo)

    def resolve(
        self,
        sessions: Sequence[UserSession],
        user: str,
        tty: str | None,
        addressed_repo: str = "",
    ) -> WriteTarget:
        """Resolve a write address to its delivery target.

        A bare ``user`` targets the user's mailbox.  ``user:tty`` targets
        the session holding that tty; when nobody holds it, the message is
        forwarded to the user's mailbox in *addressed_repo* — the visible
        repo where the named session last ran — or, without one, in a repo
        where the user is present.  Raises ``ValueError`` when neither
        identifies the user, so a message never reaches a user the sender
        did not name.
        """
        if not tty:
            return WriteTarget(relay_key=user, display=user, target_repo=None)
        session = resolve_tty_name(sessions, user, tty, local_repo=self.local_repo)
        if session is not None:
            return WriteTarget(
                relay_key=build_session_key(session.user, session.tty),
                display=f"{user}:{tty}",
                target_repo=self._remote(session.repo),
            )
        if not self._is_visible(addressed_repo):
            addressed_repo = self._present_repo(sessions, user)
        where = f" in {display_repo_name(addressed_repo)}" if addressed_repo else ""
        return WriteTarget(
            relay_key=user,
            display=user,
            target_repo=self._remote(addressed_repo),
            forwarded=True,
            notice=(
                f"{tty} has no active session; delivered to {user}'s mailbox"
                f"{where} (their next session there will see it)."
            ),
        )

    def _is_visible(self, repo: str) -> bool:
        return bool(repo) and (repo == self.local_repo or repo in self.visible_repos)

    def _remote(self, repo: str) -> str | None:
        return remote_repo(repo, self.local_repo, self.visible_repos)

    def _present_repo(self, sessions: Sequence[UserSession], user: str) -> str:
        """Pick the repo whose mailbox *user* will drain, absent a named one.

        The mailbox is repo-scoped and a recipient drains only the repo
        where they are live, so prefer repos with a live session; with none
        live, any repo they are present in (a dormant session revives and
        drains it).  Within the candidates prefer the local repo, else the
        first by name, so the choice is deterministic.
        """
        theirs = [s for s in sessions if s.user == user]
        if not theirs:
            msg = f"user {user} not found in visible repos"
            raise ValueError(msg)
        candidates = {s.repo for s in (live_sessions(theirs) or theirs)}
        return self.local_repo if self.local_repo in candidates else min(candidates)
