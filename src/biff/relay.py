"""Relay protocol and local filesystem implementation.

The Relay abstracts how the MCP server communicates with the
message routing layer.  The MCP server is per-user; the relay
is shared.

Session keys are composite ``{user}:{tty}`` strings.  Each server
instance owns one session key.

``LocalRelay`` implements the relay over a shared filesystem
directory with per-session inbox files, per-user inbox files,
and a shared sessions file::

    {data_dir}/
        userinbox-kai.jsonl        # per-user mailbox (broadcast)
        inbox-kai-a1b2c3d4.jsonl   # per-TTY mailbox (targeted)
        userinbox-eric.jsonl
        inbox-eric-12345678.jsonl
        sessions.json

``NatsRelay`` (in :mod:`biff.nats_relay`) implements the same protocol
over a NATS server for networked deployments.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Protocol

from pydantic import ValidationError

from biff.local_tty_names import LocalTtyNames
from biff.mailbox_line import MailboxLine
from biff.models import (
    Message,
    SessionEvent,
    UnreadSummary,
    UserSession,
    WallPost,
)
from biff.relay_config import RelayLocation
from biff.session_lock import routing_lock
from biff.tty import build_session_key

if TYPE_CHECKING:
    from biff.session_edit import RowChange, SessionEdit

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 259_200  # 3 days — covers weekends; KV storage retention
# Presence liveness: a session is "live" only if it heartbeat within this
# window (2x the 60s heartbeat interval).  Distinct from the 3-day storage
# TTL — used to hide dead sessions from presence before their KV entry
# expires.
PRESENCE_LIVENESS_SECONDS = 120.0


def atomic_write(path: Path, content: str) -> None:
    """Atomically write *content* to *path* using temp-file-then-replace.

    Creates parent directories if needed. On failure, the temp file
    is cleaned up and the original file is left untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(content)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class Relay(Protocol):
    """Interface between an MCP server and the message relay.

    Session keys are ``{user}:{tty}`` composite strings.

    Two mailbox types exist per user:

    - **User mailbox**: receives broadcast messages (``/write user``).
      POP semantics — first reader consumes.
    - **TTY mailbox**: receives targeted messages (``/write user:tty``).
      One per session.
    """

    # Whether fetching mail removes it from the relay.  A consuming relay (NATS
    # acks on fetch and a work queue deletes on ack) leaves fetched mail nowhere
    # but the caller's hands, so a caller that may fail to deliver it must hold
    # it.  A relay that does not consume returns the same mail until marked read.
    consumes_on_fetch: bool

    # -- Messages (TTY inbox) --

    async def deliver(
        self,
        message: Message,
        *,
        sender_key: str = "",
        target_repo: str | None = None,
    ) -> None:
        """Deliver a message to the addressed user or session."""
        ...

    async def fetch(self, session_key: str) -> list[Message]:
        """Return unread messages from the targeted session inbox."""
        ...

    async def mark_read(self, session_key: str, ids: Sequence[uuid.UUID]) -> None:
        """Acknowledge the selected session messages."""
        ...

    async def get_unread_summary(self, session_key: str) -> UnreadSummary:
        """Return the unread count for a session."""
        ...

    async def inbox_high_water(self, session_key: str) -> int:
        """Return a number that rises whenever mail arrives for the session.

        It orders arrivals and is never a count: a read may lower it, and it is
        0 for an inbox that has never held mail.  A mail that arrives later is
        always above every value returned earlier, even when the unread count
        is flat (two read, two arrive).
        """
        ...

    # -- Messages (user inbox) --

    async def fetch_user_inbox(self, user: str) -> list[Message]:
        """Return unread broadcast messages from the shared user inbox."""
        ...

    async def mark_read_user_inbox(self, user: str, ids: Sequence[uuid.UUID]) -> None:
        """Acknowledge the selected broadcast messages."""
        ...

    async def get_user_unread_count(self, user: str) -> int:
        """Return the number of unread broadcast messages."""
        ...

    # -- Presence --

    async def update_session(self, session: UserSession) -> None:
        """Store the current presence record for a session."""
        ...

    async def get_session(self, session_key: str) -> UserSession | None:
        """Return the session record, or None if it is absent."""
        ...

    async def update_session_if(self, edit: SessionEdit) -> UserSession | None:
        """Atomically store ``edit.change(row)``; the row, or None if absent."""
        ...

    async def get_sessions_for_user(self, user: str) -> list[UserSession]:
        """Return the user's registered sessions."""
        ...

    async def heartbeat(self, session_key: str, at: datetime | None = None) -> None:
        """Refresh liveness; advance ``last_tool_at`` to *at* if that is newer."""
        ...

    async def get_sessions(self) -> list[UserSession]:
        """Return registered sessions visible to this relay."""
        ...

    async def get_sessions_for_repos(self, repos: frozenset[str]) -> list[UserSession]:
        """Return sessions in the selected repositories."""
        ...

    async def delete_session(self, session_key: str) -> None:
        """Remove a session from presence."""
        ...

    # -- Session history (wtmp) --

    async def append_wtmp(self, event: SessionEvent) -> None:
        """Append a login or logout event to session history."""
        ...

    async def get_wtmp(
        self, *, user: str | None = None, count: int = 25
    ) -> list[SessionEvent]:
        """Return recent session events, optionally filtered by user."""
        ...

    async def last_session_repo(self, address: str) -> str | None:
        """Return the repo of the latest event for session ``user:tty``."""
        ...

    # -- Wall (team broadcast) --

    async def set_wall(self, wall: WallPost | None) -> None:
        """Store the team wall post, or clear it when given None."""
        ...

    async def get_wall(self, *, repo: str | None = None) -> WallPost | None:
        """Return the active wall post, or None when it is absent."""
        ...

    # -- TTY name reservation (DES-035) --

    async def reserve_tty_name(self, user: str, name: str, session_key: str) -> bool:
        """Claim a name for a session and report whether it succeeded."""
        ...

    async def release_tty_name(self, user: str, name: str) -> None:
        """Release the user's reserved name."""
        ...

    async def release_tty_name_if(self, user: str, name: str, owner: str) -> bool:
        """Release *name* only if *owner* still holds it; say whether it did."""
        ...

    async def refresh_tty_reservation(
        self, user: str, name: str, session_key: str
    ) -> None:
        """Refresh the name reservation held by a session."""
        ...

    async def get_tty_reservation_owner(self, user: str, name: str) -> str | None:
        """Return the owning session key, or None if unreserved."""
        ...

    async def list_reserved_names(self, user: str) -> list[str]:
        """Return the user's reserved session names."""
        ...

    # -- session_id -> last tty-name hint (resume reclaim) --

    async def get_session_tty_hint(self, user: str, session_id: str) -> str | None:
        """Return the session's last name hint, or None if absent."""
        ...

    async def set_session_tty_hint(self, user: str, session_id: str, name: str) -> None:
        """Store a name hint for a resumed session."""
        ...

    # -- Lifecycle --

    async def disconnect(self) -> None:
        """Release the connection temporarily.  Next relay call reconnects."""
        ...

    async def close(self) -> None:
        """Do nothing while disabled."""
        ...


class DormantRelay:
    """Null relay for disabled biff instances.

    Implements the full :class:`Relay` protocol with safe empty returns.
    Tools bound to a dormant relay get no messages, no sessions, and no
    errors — the server runs but does nothing on the network.
    """

    consumes_on_fetch = False  # there is no mail to fetch

    async def deliver(
        self,
        message: Message,
        *,
        sender_key: str = "",
        target_repo: str | None = None,
    ) -> None:
        """Do nothing while disabled."""

    async def fetch(self, session_key: str) -> list[Message]:  # noqa: ARG002 — Protocol impl
        """Return no targeted messages while disabled."""
        return []

    async def mark_read(self, session_key: str, ids: Sequence[uuid.UUID]) -> None:
        """Do nothing while disabled."""

    async def get_unread_summary(self, session_key: str) -> UnreadSummary:  # noqa: ARG002
        """Return an empty result while disabled."""
        return UnreadSummary()

    async def inbox_high_water(self, session_key: str) -> int:
        """Return 0: no mail arrives while disabled."""
        del session_key
        return 0

    async def fetch_user_inbox(self, user: str) -> list[Message]:  # noqa: ARG002
        """Return an empty result while disabled."""
        return []

    async def mark_read_user_inbox(self, user: str, ids: Sequence[uuid.UUID]) -> None:
        """Do nothing while disabled."""

    async def get_user_unread_count(self, user: str) -> int:  # noqa: ARG002
        """Return an empty result while disabled."""
        return 0

    async def update_session(self, session: UserSession) -> None:
        """Do nothing while disabled."""

    async def get_session(self, session_key: str) -> UserSession | None:  # noqa: ARG002
        """Return no session while disabled."""
        return None

    async def update_session_if(self, edit: SessionEdit) -> UserSession | None:
        """Return no session while disabled."""
        del edit
        return None

    async def get_sessions_for_user(self, user: str) -> list[UserSession]:  # noqa: ARG002
        """Return an empty result while disabled."""
        return []

    async def heartbeat(self, session_key: str, at: datetime | None = None) -> None:
        """Do nothing while disabled."""

    async def get_sessions(self) -> list[UserSession]:
        """Return an empty result while disabled."""
        return []

    async def get_sessions_for_repos(self, repos: frozenset[str]) -> list[UserSession]:  # noqa: ARG002
        """Return an empty result while disabled."""
        return []

    async def delete_session(self, session_key: str) -> None:
        """Do nothing while disabled."""

    async def append_wtmp(self, event: SessionEvent) -> None:
        """Do nothing while disabled."""

    async def get_wtmp(
        self,
        *,
        user: str | None = None,  # noqa: ARG002
        count: int = 25,  # noqa: ARG002
    ) -> list[SessionEvent]:
        """Return an empty result while disabled."""
        return []

    async def last_session_repo(self, address: str) -> str | None:  # noqa: ARG002
        """Return no repo while disabled."""
        return None

    async def set_wall(self, wall: WallPost | None) -> None:
        """Do nothing while disabled."""

    async def get_wall(self, *, repo: str | None = None) -> WallPost | None:  # noqa: ARG002
        """Return an empty result while disabled."""
        return None

    async def reserve_tty_name(self, user: str, name: str, session_key: str) -> bool:  # noqa: ARG002
        """Accept the reservation without storing it while disabled."""
        return True

    async def release_tty_name(self, user: str, name: str) -> None:
        """Do nothing while disabled."""

    async def release_tty_name_if(self, user: str, name: str, owner: str) -> bool:
        """Hold no name while disabled."""
        del user, name, owner
        return False

    async def refresh_tty_reservation(
        self, user: str, name: str, session_key: str
    ) -> None:
        """Do nothing while disabled."""

    async def get_tty_reservation_owner(self, user: str, name: str) -> str | None:  # noqa: ARG002
        """Return an empty result while disabled."""
        return None

    async def list_reserved_names(self, user: str) -> list[str]:  # noqa: ARG002
        """Return an empty result while disabled."""
        return []

    async def get_session_tty_hint(self, user: str, session_id: str) -> str | None:  # noqa: ARG002
        """Return an empty result while disabled."""
        return None

    async def set_session_tty_hint(self, user: str, session_id: str, name: str) -> None:
        """Do nothing while disabled."""

    async def disconnect(self) -> None:
        """Do nothing while disabled."""

    async def close(self) -> None:
        """Do nothing while disabled."""


class LocalRelay:
    """Filesystem-backed relay with per-user and per-session inbox files.

    Broadcast messages go to the user mailbox (``inbox-{user}.jsonl``).
    Targeted messages go to the TTY mailbox (``inbox-{user}-{tty}.jsonl``).
    Sessions are stored in a single shared ``sessions.json`` keyed
    by ``{user}:{tty}``.  All writes use temp-file-then-replace for
    atomicity.
    """

    consumes_on_fetch = False  # mail stays in its inbox file until mark_read

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._names = LocalTtyNames(data_dir, self._validate_user)
        # Session keys for which heartbeat() has already logged a missing-
        # session warning -- caps the warning at once per key rather than
        # once per heartbeat tick, which runs on a fixed interval forever.
        self._heartbeat_missing_warned: set[str] = set()

    @property
    def data_dir(self) -> Path:
        """The directory this relay keeps its files in."""
        return self._data_dir

    def _lock(self) -> IO[str]:
        """Return the directory's lock file, held (``flock``) until it is closed.

        Every read-modify-write of a mailbox or the sessions file runs under it,
        so another process cannot replace the file with an older snapshot.
        """
        RelayLocation.ensure(self._data_dir)
        lock = (self._data_dir / ".lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX)
        return lock

    @staticmethod
    def _validate_user(user: str) -> str:
        """Reject usernames that could escape the data directory."""
        if not user or "/" in user or "\\" in user or ".." in user:
            msg = f"Invalid username: {user!r}"
            raise ValueError(msg)
        return user

    def _validate_session_key(self, session_key: str) -> None:
        """Reject session keys that could escape the data directory."""
        if ":" not in session_key:
            msg = f"Invalid session key (missing ':'): {session_key!r}"
            raise ValueError(msg)
        user, tty = session_key.split(":", maxsplit=1)
        self._validate_user(user)
        if not tty or "/" in tty or "\\" in tty or ".." in tty or ":" in tty:
            msg = f"Invalid tty in session key: {tty!r}"
            raise ValueError(msg)

    def _inbox_path_for_key(self, session_key: str) -> Path:
        """Inbox file path for a session key (``{user}:{tty}``)."""
        self._validate_session_key(session_key)
        safe = session_key.replace(":", "-")
        return self._data_dir / f"inbox-{safe}.jsonl"

    def _user_inbox_path(self, user: str) -> Path:
        """Inbox file path for a user's broadcast mailbox.

        Uses ``userinbox-`` prefix to avoid collision with TTY inbox
        files (``inbox-{user}-{tty}.jsonl``).
        """
        self._validate_user(user)
        return self._data_dir / f"userinbox-{user}.jsonl"

    # -- Messages --

    async def deliver(
        self,
        message: Message,
        *,
        sender_key: str = "",  # noqa: ARG002
        target_repo: str | None = None,  # noqa: ARG002
    ) -> None:
        """Deliver a message to the recipient's inbox.

        If ``to_user`` contains a ``:`` (targeted), deliver to the
        TTY inbox.  Otherwise (broadcast), deliver to the user's
        broadcast mailbox — no session lookup, persists offline.
        """
        self._validate_user(message.from_user)
        to = message.to_user  # user:tty is a session's mailbox, a bare user's
        self._validate_user(to.split(":")[0])  # is the broadcast one
        path = (self._inbox_path_for_key if ":" in to else self._user_inbox_path)(to)
        with self._lock():
            if any(m.id == message.id for m in self._read_inbox_file(path)):
                return  # same id twice is one message, as JetStream dedups it
            # Rewritten by rename, like every other change to a mailbox, so a
            # watch on the directory sees an arrival at once (an append to an
            # existing file would not change the directory).  The old text is
            # carried through as it is, so a line this version cannot parse is
            # never dropped by a delivery.
            lines = [*self._lines(path), message.model_dump_json()]
            atomic_write(path, "\n".join(lines) + "\n")
            path.chmod(0o600)

    async def fetch(self, session_key: str) -> list[Message]:
        """Get unread messages for a session, oldest first."""
        return [m for m in self._read_inbox(session_key) if not m.read]

    async def mark_read(self, session_key: str, ids: Sequence[uuid.UUID]) -> None:
        """Mark messages as read.  Rewrites the session's inbox atomically."""
        self._mark_read(self._inbox_path_for_key(session_key), ids)

    async def get_unread_summary(self, session_key: str) -> UnreadSummary:
        """Count unread messages across TTY and user inboxes."""
        user = session_key.split(":")[0]
        unread = [*await self.fetch(session_key), *await self.fetch_user_inbox(user)]
        return UnreadSummary(count=len(unread))

    async def inbox_high_water(self, session_key: str) -> int:
        """Count the message lines ever delivered to the session's two inboxes.

        A read marks a line in place and never removes one, so the number only
        rises, by exactly one for each arrival.
        """
        if not self._data_dir.is_dir():
            return 0  # no mail has ever been delivered here
        user = session_key.split(":")[0]
        paths = (self._inbox_path_for_key(session_key), self._user_inbox_path(user))
        return sum(len(self._lines(path)) for path in paths)

    # -- Messages (user inbox) --

    async def fetch_user_inbox(self, user: str) -> list[Message]:
        """Get unread messages from the user's broadcast mailbox."""
        path = self._user_inbox_path(user)
        return [m for m in self._read_inbox_file(path) if not m.read]

    async def mark_read_user_inbox(self, user: str, ids: Sequence[uuid.UUID]) -> None:
        """Mark messages as read in the user's broadcast mailbox."""
        self._mark_read(self._user_inbox_path(user), ids)

    @staticmethod
    def _lines(path: Path) -> list[str]:
        """Return the lines of *path*, or none if it does not exist."""
        return path.read_text().splitlines() if path.exists() else []

    @staticmethod
    def _marked(line: str, ids: set[uuid.UUID]) -> str:
        """Return *line* with its message marked read if it is one of *ids*."""
        msg = MailboxLine.parse(line)
        if msg and msg.id in ids:
            return msg.model_copy(update={"read": True}).model_dump_json()
        return line

    def _mark_read(self, path: Path, ids: Sequence[uuid.UUID]) -> None:
        """Mark the messages *ids* read in the inbox at *path*.

        Works on raw lines and changes only the parsed lines being marked, so a
        line this version cannot parse (from a newer biff) is kept as it is.
        """
        with self._lock():
            lines = self._lines(path)
            wanted = set(ids)
            marked = [self._marked(line, wanted) for line in lines]
            if marked != lines:
                atomic_write(path, "\n".join(marked) + "\n")
                path.chmod(0o600)

    async def get_user_unread_count(self, user: str) -> int:
        """Count unread messages in the user's broadcast mailbox."""
        return len(await self.fetch_user_inbox(user))

    # -- Presence --

    async def update_session(self, session: UserSession) -> None:
        """Create or update a session (keyed by ``{user}:{tty}``)."""
        self._validate_user(session.user)
        key = build_session_key(session.user, session.tty)
        self._validate_session_key(key)
        with self._lock():
            sessions = self._read_sessions()
            sessions[key] = session
            self._write_sessions(sessions)

    def _is_expired(self, session: UserSession) -> bool:
        """Check if a session has exceeded the idle TTL."""
        age = (datetime.now(UTC) - session.last_active).total_seconds()
        return age > SESSION_TTL_SECONDS

    async def get_session(self, session_key: str) -> UserSession | None:
        """Get a specific session by its ``{user}:{tty}`` key."""
        session = self._read_sessions().get(session_key)
        if session is not None and self._is_expired(session):
            return None
        return session

    async def get_sessions_for_user(self, user: str) -> list[UserSession]:
        """Get all sessions for a given user, reaping removals first."""
        self._validate_user(user)
        self.reap_sentinels()
        prefix = f"{user}:"
        return [
            s
            for k, s in self._read_sessions().items()
            if k.startswith(prefix) and not self._is_expired(s)
        ]

    async def heartbeat(self, session_key: str, at: datetime | None = None) -> None:
        """Update ``last_active`` for an existing session; skip if missing.

        Matches :meth:`NatsRelay.heartbeat`: a missing session (expired,
        deleted, or not yet created) is skipped rather than replaced with
        a bare ``UserSession(user, tty)``, which would destroy
        ``tty_name``, ``repo``, ``pwd``, ``hostname``, ``plan``, and every
        other field that only the lifespan or a tool handler knows how
        to set.

        Deliberately touches only ``last_active`` (liveness) — never
        ``last_tool_at``, the idle time ``/who``/``/finger`` display.
        ``model_copy(update=...)`` below only overwrites the keys named
        in its ``update`` mapping, so ``last_tool_at`` on the existing
        session survives unchanged.
        """
        if self._edit_row(session_key, lambda row: row.beat(at)) is None:
            # A session vanishing under a running heartbeat loop is
            # anomalous (expired, deleted, or never created) -- warn once
            # per key, not on every tick, since the loop runs on a fixed
            # interval for the lifetime of the process.
            if session_key not in self._heartbeat_missing_warned:
                self._heartbeat_missing_warned.add(session_key)
                logger.warning("Heartbeat found no session; skipping")
        else:
            self._heartbeat_missing_warned.discard(session_key)

    async def update_session_if(self, edit: SessionEdit) -> UserSession | None:
        """Store ``edit.change(row)`` under the relay's file lock."""
        return self._edit_row(edit.key, edit.change)

    def _edit_row(self, key: str, change: RowChange) -> UserSession | None:
        self._validate_session_key(key)
        with self._lock():
            sessions = self._read_sessions()
            existing = sessions.get(key)
            if existing is None:
                return None
            updated = change(existing)
            if updated is not existing:
                sessions[key] = updated
                self._write_sessions(sessions)
            return updated

    async def get_sessions(self) -> list[UserSession]:
        """Get all sessions, reaping removals and filtering expired."""
        self.reap_sentinels()
        return [s for s in self._read_sessions().values() if not self._is_expired(s)]

    async def get_sessions_for_repos(
        self,
        repos: frozenset[str],  # noqa: ARG002
    ) -> list[UserSession]:
        """LocalRelay is single-repo — returns same as get_sessions()."""
        return await self.get_sessions()

    async def delete_session(self, session_key: str) -> None:
        """Remove a session from storage, under the directory lock.

        The read-modify-write of ``sessions.json`` must not interleave with
        another process's :meth:`update_session`, or the later rename loses its
        update.  :meth:`delete_session_sync` stays lock-free for the signal
        handler, which must not block.
        """
        with self._lock():
            self.delete_session_sync(session_key)

    def delete_session_sync(self, session_key: str) -> None:
        """Remove a session from storage (sync, safe from signal handlers)."""
        self._validate_session_key(session_key)
        try:
            sessions = self._parse_sessions_file()
        except (ValidationError, ValueError, json.JSONDecodeError, AttributeError):
            # No logger here: unlike _read_sessions, this method also runs
            # from inside a signal handler's cleanup step
            # (server.app._run_signal_cleanup_steps), where a logging call
            # can deadlock on the module's lock.  A corrupt file has no
            # valid session to remove anyway, so swallowing silently here
            # is honest -- the caller only wanted this one key gone.
            return
        if session_key in sessions:
            del sessions[session_key]
            self._write_sessions(sessions)

    def write_remove_sentinel(self, session_key: str) -> None:
        """Create a sentinel file marking a session for removal.

        The sentinel is a plain file whose content is the session key.
        Any server that calls :meth:`reap_sentinels` (via
        :meth:`get_sessions` or :meth:`get_sessions_for_user`) will
        delete the corresponding session and clean up the file.

        Safe to call from signal handlers — sync I/O only, no
        read-modify-write on the shared sessions file.
        """
        self._validate_session_key(session_key)
        RelayLocation.ensure(self._data_dir)
        safe = session_key.replace(":", "-")
        sentinel = self._data_dir / f"remove-{safe}"
        sentinel.write_text(session_key)
        sentinel.chmod(0o600)

    def reap_sentinels(self) -> None:
        """Process sentinel files, removing flagged sessions.

        Reads each ``remove-*`` file, deletes the named session from
        ``sessions.json``, and removes the sentinel.  Called
        automatically by :meth:`get_sessions` and
        :meth:`get_sessions_for_user` so callers always see clean data.
        """
        if not self._data_dir.exists():
            return
        sentinels = list(self._data_dir.glob("remove-*"))
        if not sentinels:
            return
        for sentinel in sentinels:
            try:
                session_key = sentinel.read_text().strip()
            except OSError:
                continue
            except UnicodeError:
                # A truncated/corrupt signal-handler artifact names no key.
                sentinel.unlink(missing_ok=True)
                continue
            try:
                self._validate_session_key(session_key)
            except ValueError:
                # Validate before taking a lock or attempting deletion. A
                # crash between open and write can leave an empty sentinel;
                # discard only this artifact, never block neighboring keys.
                sentinel.unlink(missing_ok=True)
                continue
            with routing_lock(session_key.partition(":")[2]) as acquired:
                if not acquired:
                    continue
                with self._lock():
                    self.delete_session_sync(session_key)
                sentinel.unlink(missing_ok=True)

    # -- Session history (wtmp) --

    async def append_wtmp(self, event: SessionEvent) -> None:
        """No-op — local relay does not persist session history."""

    async def get_wtmp(
        self,
        *,
        user: str | None = None,  # noqa: ARG002
        count: int = 25,  # noqa: ARG002
    ) -> list[SessionEvent]:
        """Return empty list — local relay does not persist session history."""
        return []

    async def last_session_repo(self, address: str) -> str | None:  # noqa: ARG002
        """Return no repo — local relay does not persist session history."""
        return None

    # -- Wall (team broadcast) --

    async def set_wall(self, wall: WallPost | None) -> None:
        """Set or clear the team wall broadcast."""
        path = self._data_dir / "wall.json"
        if wall is None:
            path.unlink(missing_ok=True)
        else:
            RelayLocation.ensure(self._data_dir)
            atomic_write(path, wall.model_dump_json() + "\n")
            path.chmod(0o600)

    async def get_wall(self, *, repo: str | None = None) -> WallPost | None:  # noqa: ARG002
        """Read the active wall, returning ``None`` if absent or expired."""
        path = self._data_dir / "wall.json"
        try:  # a missing file is an OSError too
            wall = WallPost.model_validate_json(path.read_text())
        except (ValidationError, ValueError, OSError):
            return None
        if wall.is_expired:
            path.unlink(missing_ok=True)
            return None
        return wall

    # -- TTY name reservation (DES-035) and reclaim hints --

    async def reserve_tty_name(self, user: str, name: str, session_key: str) -> bool:
        """Reserve a TTY name atomically via O_CREAT|O_EXCL lockfile."""
        with self._lock():
            return self._names.reserve(user, name, session_key)

    async def release_tty_name(self, user: str, name: str) -> None:
        """Release a TTY name reservation by removing the lockfile."""
        with self._lock():
            self._names.release(user, name)

    async def release_tty_name_if(self, user: str, name: str, owner: str) -> bool:
        """Release *name* if *owner* holds it, under the relay's file lock."""
        with self._lock():
            return self._names.release_if(user, name, owner)

    async def refresh_tty_reservation(
        self, user: str, name: str, session_key: str
    ) -> None:
        """Refresh the reservation, if *session_key* still owns it."""
        with self._lock():
            self._names.refresh(user, name, session_key)

    async def get_tty_reservation_owner(self, user: str, name: str) -> str | None:
        """Return the session key that holds *name*, or ``None``."""
        return self._names.owner(user, name)

    async def list_reserved_names(self, user: str) -> list[str]:
        """List reserved TTY names for a user via glob on lockfiles."""
        return self._names.reserved(user)

    async def get_session_tty_hint(self, user: str, session_id: str) -> str | None:
        """Return the last tty_name this session_id claimed, or ``None``."""
        return self._names.hint(user, session_id)

    async def set_session_tty_hint(self, user: str, session_id: str, name: str) -> None:
        """Record the tty_name this session_id claimed (overwrites)."""
        self._names.set_hint(user, session_id, name)

    async def disconnect(self) -> None:
        """No-op — filesystem relay has no connection to release."""

    async def close(self) -> None:
        """No-op — filesystem relay has no connection to close."""

    # -- Internal I/O --

    @staticmethod
    def _read_inbox_file(path: Path) -> list[Message]:
        """Read all messages from an inbox file."""
        lines = LocalRelay._lines(path)
        return [m for m in map(MailboxLine.parse, lines) if m is not None]

    def _read_inbox(self, session_key: str) -> list[Message]:
        """Read all messages from a session's TTY inbox."""
        return self._read_inbox_file(self._inbox_path_for_key(session_key))

    def _parse_sessions_file(self) -> dict[str, UserSession]:
        """Parse sessions.json, raising on corruption.

        No logging and no fallback here -- callers decide how to handle
        corruption.  Split out of ``_read_sessions`` so
        ``delete_session_sync`` (also reachable from a signal handler's
        cleanup step) can swallow corruption without calling ``logger``.
        """
        path = self._data_dir / "sessions.json"
        if not path.exists():
            return {}
        data = json.loads(path.read_text())
        return {k: UserSession.model_validate(v) for k, v in data.items()}

    def _read_sessions(self) -> dict[str, UserSession]:
        """Read all sessions, logging (and starting fresh) on corruption."""
        try:
            return self._parse_sessions_file()
        except (ValidationError, ValueError, json.JSONDecodeError, AttributeError):
            logger.warning("Corrupt sessions file, starting fresh")
            return {}

    def _write_sessions(self, sessions: dict[str, UserSession]) -> None:
        """Atomically rewrite the sessions file."""
        data = {k: v.model_dump(mode="json") for k, v in sessions.items()}
        RelayLocation.ensure(self._data_dir)
        path = self._data_dir / "sessions.json"
        atomic_write(path, json.dumps(data, indent=2) + "\n")
        path.chmod(0o600)
