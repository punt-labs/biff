"""Relay-aware lifecycle of CLI sessions: create, attach, end, and expiry."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from biff.cli_session_select import SessionSelectionError, SessionSelector
from biff.cli_session_store import (
    CliSessionEndedError,
    CliSessionRecord,
)
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
            except CliSessionEndedError:
                logger.info("pointed session %s ended; creating one", pointed.tty)
        return await self.attach(await self.create())

    async def create(self, tty: str | None = None) -> CliSessionRecord:
        """Create a live session, reserve its name, register it, and point at it.

        An exact *tty* that is taken raises ``ValueError`` and creates nothing.
        """
        if tty is not None and (error := validate_tty_name(tty)):
            raise ValueError(error)
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
        sel.pointer.write(sel.repo_root, record.session_id)
        return record

    async def attach(self, record: CliSessionRecord) -> Attachment:
        """Revive or renew *record* and refresh its relay row."""
        renewed = self.selector.store.attach(record.session_id, now=datetime.now(UTC))
        await self._register(renewed)
        return Attachment(self._routing(renewed), renewed.tty, renewed)

    async def end(self, record: CliSessionRecord) -> CliSessionRecord:
        """End *record*: drain its inbox, release its name and row, mark ended."""
        await self._release(record)
        await self._wtmp(record, "logout")
        return self.selector.store.end(record.session_id, now=datetime.now(UTC))

    async def sweep(self) -> None:
        """Lapse idle sessions to dormant and expire those past retention.

        Housekeeping must never block the command that triggered it, so a
        failure on one record is logged and the sweep moves on.
        """
        store = self.selector.store
        now = datetime.now(UTC)
        for record in store.list():
            if record.user != self.config.user or record.hostname != self.hostname:
                continue
            try:
                if record.state == "live" and now - record.last_attached_at > LEASE:
                    store.lapse(record.session_id)
                elif record.state != "live" and (
                    now - (record.ended_at or record.last_attached_at) > RETENTION
                ):
                    await self._release(record)
                    store.expire(record.session_id)
            except Exception:
                logger.warning(
                    "session sweep failed for %s", record.session_id, exc_info=True
                )

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
        """Write the relay row for *record*, keeping the row's other fields."""
        key = self._key(record)
        now = datetime.now(UTC)
        row = await self.relay.get_session(key) or UserSession(
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
        await self._drain(record.user, key)
        owner = await self.relay.get_tty_reservation_owner(record.user, record.tty)
        if owner == key:
            await self.relay.release_tty_name(record.user, record.tty)
        await self.relay.delete_session(key)

    async def _drain(self, user: str, key: str) -> None:
        """Re-file every unread message for *key* into *user*'s mailbox.

        A message is in exactly one place afterwards.  If the mailbox write
        fails part-way, the messages not yet moved go back to the session
        inbox so nothing is lost, and the failure propagates.
        """
        pending = await self.relay.fetch(key)
        moved: list[uuid.UUID] = []
        try:
            for message in pending:
                await self.relay.deliver(
                    message.model_copy(
                        update={"id": uuid.uuid4(), "to_user": user, "read": False}
                    )
                )
                moved.append(message.id)
        except BaseException:
            for message in pending:
                if message.id not in moved:
                    await self.relay.deliver(
                        message.model_copy(update={"id": uuid.uuid4()})
                    )
            raise
        await self.relay.mark_read(key, moved)

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
            logger.warning("failed to write wtmp %s", event, exc_info=True)
