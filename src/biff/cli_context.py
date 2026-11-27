"""The context a CLI command runs in: relay connection plus session identity."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Self

from biff.models import BiffConfig
from biff.relay import Relay
from biff.talk_state import TalkState


@dataclass(frozen=True)
class CliContext:
    """Context for a CLI command: relay connection + identity."""

    relay: Relay
    config: BiffConfig
    session_key: str
    user: str
    tty: str
    tty_name: str = ""
    org_repos: frozenset[str] = frozenset()
    repo_root: Path | None = None
    # The stored CLI session's id; ``None`` for a client of a harness session.
    # ``biff plan`` keys its plan-gate marker on it.
    record_session_id: str | None = None
    # Renames the stored CLI session record; ``None`` for a client of a harness
    # session, which has no record of its own.
    rename_record: Callable[[str], Awaitable[None]] | None = None
    talk: TalkState = field(init=False)

    def __post_init__(self) -> None:
        """Compose the shared ephemeral talk state from this session's identity.

        ``TalkState`` is mutable and held by reference inside the frozen
        context — the same seating ``ServerState`` gives its
        ``ActivityTracker``.  Both front-ends feed it every talk
        notification and drain it in their own idiom (talk_state.py).
        """
        object.__setattr__(
            self,
            "talk",
            TalkState(
                relay=self.relay,
                user=self.user,
                tty=self.tty,
                session_key=self.session_key,
                tty_name=self.tty_name,
            ),
        )

    def with_tty_name(self, tty_name: str) -> Self:
        """Return this context under a new display tty name, with the same talk state.

        ``dataclasses.replace`` builds a fresh context, whose ``__post_init__``
        would seat a new, empty ``TalkState`` and lose a conversation in
        progress.  A context that outlives its first change (a daemon session)
        changes through these methods instead.
        """
        self.talk.set_tty_name(tty_name)
        return self._carrying_talk(replace(self, tty_name=tty_name))

    def with_org_repos(self, org_repos: frozenset[str]) -> Self:
        """Return this context seeing *org_repos*, with the same talk state."""
        return self._carrying_talk(replace(self, org_repos=org_repos))

    def _carrying_talk(self, evolved: Self) -> Self:
        object.__setattr__(evolved, "talk", self.talk)
        return evolved

    @property
    def visible_repos(self) -> frozenset[str]:
        """All repos visible: self + explicit peers + org-discovered (DES-034)."""
        if not self.org_repos:
            return self.config.visible_repos
        return self.config.visible_repos | self.org_repos
