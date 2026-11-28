"""Relay-aware lifecycle of CLI sessions: create, attach, end, and expiry."""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from biff.cli_context import CliContext
from biff.daemon.session_drain import InboxDrain
from biff.daemon.session_select import SessionSelectionError, SessionSelector
from biff.daemon.session_store import CliSessionEndedError, CliSessionRecord
from biff.models import BiffConfig, SessionEvent, UserSession
from biff.relay import SESSION_TTL_SECONDS, Relay
from biff.session_id import CLI_ROLE
from biff.session_routing import SessionRouting
from biff.tty import claim_tty_name, validate_tty_name

logger = logging.getLogger(__name__)

LEASE = timedelta(hours=8)
"""A session with no attach for this long moves from live to dormant."""

RETENTION = timedelta(seconds=SESSION_TTL_SECONDS)
"""A dormant or ended record is removed this long after it was last used."""


@dataclass(frozen=True, slots=True)
class Attachment:
    """The session one invocation runs as.

    ``record`` is ``None`` when the invocation is a client of a harness
    session, which owns its own record and presence row.
    """

    routing: SessionRouting
    tty_name: str
    record: CliSessionRecord | None


@dataclass(frozen=True, slots=True)
class SessionHost:
    """Creates, attaches to, ends and expires the CLI sessions of one user.

    Owns every relay side effect of the session lifecycle so the filesystem
    store stays relay-free.  A session's relay row and tty name are released
    only by :meth:`end` or retention expiry, never by an invocation exiting.
    """

    relay: Relay
    config: BiffConfig
    selector: SessionSelector
    hostname: str
    pwd: str

    async def resolve(self, ref: str | None) -> Attachment:
        """Return the session for *ref* by the five-rung default.

        An explicit *ref* (``-s`` or ``BIFF_SESSION``) errors when it cannot
        be honored and never creates.  Otherwise a harness-declared session,
        then the repo pointer, then a fresh session.
        """
        if ref is not None:
            return await self.attach(self.selector.explicit(ref))
        harness = SessionRouting.declared_by_harness()
        if harness is not None and (client := await self._client_of(harness)):
            return client
        pointed = self.selector.pointed()
        if pointed is not None:
            try:
                return await self.attach(pointed)
            except (CliSessionEndedError, FileNotFoundError):
                # Ended, or expired between the pointer read and the attach lock.
                logger.info("pointed session %s unusable; creating one", pointed.tty)
        record = await self.create()
        self.selector.pointer.write(self.selector.repo_root, record.session_id)
        return await self.attach(record)

    async def create(self, tty: str | None = None) -> CliSessionRecord:
        """Create a live session, reserve its name, and register it.

        The caller decides whether the repo points at it.

        An exact *tty* that is taken raises ``ValueError`` and creates nothing.
        """
        if tty is not None and (error := validate_tty_name(tty)):
            raise ValueError(error)
        if tty is not None:
            CliSessionRecord.require_unaliased(tty)
        sel = self.selector
        draft = CliSessionRecord.new(
            tty="pending",
            user=sel.user,
            repo=sel.repo,
            repo_root=str(sel.repo_root),
            hostname=self.hostname,
            now=datetime.now(UTC),
        )
        key = self._key(draft)
        name = await claim_tty_name(self.relay, sel.user, key, preferred=tty)
        record = replace(draft, tty=name)
        try:
            await self._write_row(record)
            sel.store.create(record)
        except BaseException:
            await self.relay.delete_session(key)
            await self.relay.release_tty_name(sel.user, name)
            raise
        await self._wtmp(record, "login")
        return record

    async def attach(self, record: CliSessionRecord) -> Attachment:
        """Revive or renew *record* and refresh its relay row.

        The session lock is held across the store transition and the relay
        write, so a concurrent ``end`` or sweep either finishes first (and this
        raises the ended error) or waits for the row to be written.
        """
        store = self.selector.store
        async with store.lifecycle(record.session_id):
            renewed = store.attach(record.session_id, now=datetime.now(UTC))
            await self._register(renewed)
        return Attachment(self._routing(renewed), renewed.tty, renewed)

    def context_for(
        self,
        attached: Attachment,
        *,
        relay: Relay | None = None,
        org_repos: frozenset[str] = frozenset(),
        repo_root: Path | None = None,
    ) -> CliContext:
        """Return the command context for *attached*.

        A CLI session's context can rename its stored record; a client of a
        harness session cannot, because it has none.
        """
        record = attached.record
        # A CLI session keeps the identity it was created with (frozen at
        # creation); only a harness client acts as the invocation's user.
        user = self.config.user if record is None else record.user
        return CliContext(
            relay=relay or self.relay,
            config=self.config,
            session_key=attached.routing.session_key(user),
            user=user,
            tty=attached.routing.routing_id,
            tty_name=attached.tty_name,
            org_repos=org_repos,
            repo_root=repo_root,
            record_session_id=None if record is None else record.session_id,
            rename_record=(
                None
                if record is None
                else functools.partial(self._rename, record.session_id)
            ),
        )

    async def _rename(self, session_id: str, tty: str) -> None:
        store = self.selector.store
        async with store.lifecycle(session_id):
            store.rename(session_id, tty)

    async def renew(self, record: CliSessionRecord) -> None:
        """Extend *record*'s lease while a process stays attached to it."""
        store = self.selector.store
        async with store.lifecycle(record.session_id):
            store.attach(record.session_id, now=datetime.now(UTC))

    async def end(self, record: CliSessionRecord) -> CliSessionRecord:
        """End *record*: drain its inbox, release its name and row, mark ended.

        Idempotent: a record another process already ended is returned as it
        is, with no relay change, so a late ``end`` cannot disturb a name the
        first one released and someone else has since taken.
        """
        store = self.selector.store
        async with store.lifecycle(record.session_id):
            current = store.read(record.session_id)
            if current.state == "ended":
                return current
            await self._release(current)
            await self._wtmp(current, "logout")
            return store.end(record.session_id, now=datetime.now(UTC))

    async def sweep(self) -> None:
        """Lapse idle sessions to dormant and expire those past retention.

        Only this repo's sessions are touched: another repo's inbox and
        presence live in a namespace this relay connection does not address.
        Housekeeping must never block the command that triggered it, so a
        failure is logged and the sweep moves on.  Each record is re-read
        under its lock, so a session attached or ended since the listing is
        judged on its current state.
        """
        try:
            records = self.selector.store.list()
        except OSError:
            logger.warning("session sweep could not list sessions", exc_info=True)
            return
        for record in records:
            if (
                record.user != self.config.user
                or record.hostname != self.hostname
                or record.repo != self.config.repo_name
            ):
                continue
            try:
                await self.sweep_one(record.session_id)
            except Exception:
                logger.warning(
                    "session sweep failed for %s", record.session_id, exc_info=True
                )

    async def sweep_one(self, session_id: str) -> None:
        """Lapse *session_id* to dormant or expire it, judged on its current state."""
        store = self.selector.store
        async with store.lifecycle(session_id):
            try:
                record = store.read(session_id)
            except FileNotFoundError:
                return  # expired by another process since the listing
            now = datetime.now(UTC)
            if record.state == "live":
                if now - record.last_attached_at > LEASE:
                    store.lapse(session_id)
            elif now - (record.ended_at or record.last_attached_at) > RETENTION:
                if record.state != "ended":
                    await self._release(record)
                store.expire(session_id)

    def _routing(self, record: CliSessionRecord) -> SessionRouting:
        return SessionRouting(record.session_id, role=CLI_ROLE)

    def _key(self, record: CliSessionRecord) -> str:
        return self._routing(record).session_key(record.user)

    async def _register(self, record: CliSessionRecord) -> None:
        """Re-hold the name (it may have lapsed) and refresh the row."""
        key = self._key(record)
        try:
            await claim_tty_name(self.relay, record.user, key, preferred=record.tty)
        except ValueError as exc:
            msg = f"tty name {record.tty} is held by another session"
            raise SessionSelectionError(msg) from exc
        await self._write_row(record)

    async def _write_row(self, record: CliSessionRecord) -> None:
        """Write *record*'s relay row; an attach is a command, so activity."""
        now = datetime.now(UTC)
        row = await self.relay.get_session(self._key(record)) or UserSession(
            user=record.user,
            tty=self._routing(record).routing_id,
            display_name=self.config.display_name,
            kind=self.config.kind,
            launched_by=self.config.launched_by,
            owner_git=self.config.owner_git,
            repo=record.repo,
            last_tool_at=now,
        )
        await self.relay.update_session(
            row.model_copy(
                update={
                    "tty_name": record.tty,
                    "hostname": self.hostname,
                    "pwd": self.pwd,
                    "last_active": now,
                    "last_tool_at": now,
                }
            )
        )

    async def _client_of(self, routing: SessionRouting) -> Attachment | None:
        """Attach to a harness session as a client; create nothing.

        ``None`` when this user has no row for it (a different ``--user``, or
        a session the harness never registered): the target is missing, so
        the caller falls through to the next rung.
        """
        key = routing.session_key(self.config.user)
        row = await self.relay.get_session(key)
        if row is None:
            return None
        await self.relay.heartbeat(key)
        return Attachment(routing, row.tty_name, None)

    async def _release(self, record: CliSessionRecord) -> None:
        """Drain the inbox into the owner's mailbox, then free the name and row."""
        key = self._key(record)
        await InboxDrain(self.relay).move(key, record.user)
        await self.relay.release_tty_name_if(record.user, record.tty, key)
        await self.relay.delete_session(key)

    async def _wtmp(self, record: CliSessionRecord, event: str) -> None:
        try:
            await self.relay.append_wtmp(
                SessionEvent(
                    session_key=self._key(record),
                    event=event,
                    user=record.user,
                    tty=self._routing(record).routing_id,
                    tty_name=record.tty,
                    hostname=self.hostname,
                    pwd=str(Path(self.pwd)),
                    repo=record.repo,
                )
            )
        except Exception:
            logger.warning("failed to write wtmp event", exc_info=True)
