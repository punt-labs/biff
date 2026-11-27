"""Find the stored CLI session an invocation names or a repo points at."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Self

from biff.config import ResolvedConfig
from biff.daemon.session_pointer import CurrentPointer
from biff.daemon.session_store import CliSessionRecord, CliSessionStore
from biff.tty import get_hostname


class SessionSelectionError(ValueError):
    """An explicitly named session cannot be used by this invocation."""


@dataclass(frozen=True, slots=True)
class SessionSelector:
    """Resolves a name, an id, or the repo pointer to a record on this host.

    Filesystem only.  ``user`` is the identity the invocation resolved, so a
    session owned by someone else is an error rather than an override.
    """

    store: CliSessionStore
    pointer: CurrentPointer
    user: str
    repo: str
    repo_root: Path
    hostname: str

    @classmethod
    def from_resolved(cls, resolved: ResolvedConfig) -> Self:
        """Build the selector for the identity and repo *resolved* names."""
        return cls(
            store=CliSessionStore.default(),
            pointer=CurrentPointer.default(),
            user=resolved.config.user,
            repo=resolved.config.repo_name,
            # Common root, so a linked worktree and the main checkout share one
            # default session.
            repo_root=resolved.repo_common_root,
            hostname=get_hostname(),
        )

    def lookup(self, ref: str) -> CliSessionRecord:
        """Return this user's record named *ref* (tty name or id) on this host.

        A live or dormant record wins over an ended one that reused the name.
        """
        here = self._named(ref)
        mine = [r for r in here if r.user == self.user]
        if not mine:
            raise self._not_found(ref, here)
        return self._pick(ref, mine)

    def _named(self, ref: str) -> list[CliSessionRecord]:
        return [
            r
            for r in self.store.list()
            if r.hostname == self.hostname and ref in (r.tty, r.session_id)
        ]

    @staticmethod
    def _pick(ref: str, mine: list[CliSessionRecord]) -> CliSessionRecord:
        open_ones = [r for r in mine if r.state != "ended"]
        if len(open_ones) > 1:
            ids = ", ".join(r.session_id for r in open_ones)
            msg = f"session name {ref} is ambiguous; use an id: {ids}"
            raise SessionSelectionError(msg)
        if open_ones:
            return open_ones[0]
        return max(mine, key=lambda r: r.ended_at or r.created_at)

    def _not_found(
        self, ref: str, others: list[CliSessionRecord]
    ) -> SessionSelectionError:
        if others:
            owner = others[0].user
            msg = (
                f"session {ref} belongs to {owner}, not {self.user}; a session "
                f"keeps the identity it was created with, so run with "
                f"--user {owner} or create a new session"
            )
            return SessionSelectionError(msg)
        return SessionSelectionError(f"unknown session: {ref}")

    def explicit(self, ref: str) -> CliSessionRecord:
        """Return the usable record named *ref*; never creates one."""
        record = self.lookup(ref)
        if record.state == "ended":
            raise record.ended_error()
        self.require_repo(record)
        return record

    def require_repo(self, record: CliSessionRecord) -> None:
        """Refuse a session that belongs to another repo."""
        if record.repo != self.repo:
            msg = (
                f"session {record.tty} belongs to repo {record.repo}, "
                f"not {self.repo}; run biff from inside {record.repo_root}"
            )
            raise SessionSelectionError(msg)

    def pointed(self) -> CliSessionRecord | None:
        """Return the repo's pointed session, or ``None`` when it is unusable.

        A missing, malformed, ended, wrong-user, wrong-repo or wrong-host
        target is stale; the caller falls through to a new session.
        """
        session_id = self.pointer.read(self.repo_root)
        if session_id is None:
            return None
        try:
            record = self.store.read(session_id)
        except (FileNotFoundError, ValueError):
            return None
        usable = (
            record.state != "ended"
            and record.user == self.user
            and record.repo == self.repo
            and record.hostname == self.hostname
        )
        return record if usable else None
