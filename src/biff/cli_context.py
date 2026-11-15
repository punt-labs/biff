"""The context a CLI command runs in: relay connection plus session identity."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

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

    @property
    def visible_repos(self) -> frozenset[str]:
        """All repos visible: self + explicit peers + org-discovered (DES-034)."""
        if not self.org_repos:
            return self.config.visible_repos
        return self.config.visible_repos | self.org_repos
