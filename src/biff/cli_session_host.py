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
from biff.models import BiffConfig, Message, SessionEvent, UserSession
from biff.relay import SESSION_TTL_SECONDS, Relay
from biff.session_id import CLI_ROLE
from biff.session_routing import SessionRouting
from biff.tty import claim_tty_name, validate_tty_name

logger = logging.getLogger(__name__)

_TRANSFER_NS = uuid.UUID("5d0c3f0e-8f4e-4f43-9a6f-0d2d6f1b7e11")
_RESTORE_NS = uuid.UUID("b3a1c7d2-2e49-4c52-8d1a-6e0f9c5a4b72")


def _transfer_id(source: uuid.UUID) -> uuid.UUID:
    """Stable id of *source*'s copy in the user mailbox."""
    return uuid.uuid5(_TRANSFER_NS, str(source))


def _restored_id(source: uuid.UUID) -> uuid.UUID:
    """Stable id of *source*'s copy put back in the session inbox."""
    return uuid.uuid5(_RESTORE_NS, str(source))


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
                await self._sweep_one(record.session_id)
            except Exception:
                logger.warning(
                    "session sweep failed for %s", record.session_id, exc_info=True
                )

    async def _sweep_one(self, session_id: str) -> None:
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
        """Move every unread message for *key* into *user*'s mailbox.

        ``fetch`` returns one batch at a time, so this loops until a fetch
        yields nothing new.  Each message is delivered to the mailbox under an
        id derived from its own, and only then marked read, so a retry of the
        same transfer is deduplicated by the relay instead of copied twice.

        Relays differ in what ``fetch`` does.  ``LocalRelay`` leaves messages
        in place, so after a failure the unmoved ones are still there.  The
        NATS relay consumes them as it returns them: once ``fetch`` has
        returned, the in-memory batch is the only copy, and a hard crash
        (SIGKILL, power loss) before the loop finishes loses the unmoved mail.
        The Relay API has no acknowledge-after-write to close that window.
        A failure that can be caught is handled in :meth:`_restore`.
        """
        moved: set[uuid.UUID] = set()
        while batch := [m for m in await self.relay.fetch(key) if m.id not in moved]:
            unmoved = list(batch)
            try:
                for message in batch:
                    await self.relay.deliver(
                        message.model_copy(
                            update={
                                "id": _transfer_id(message.id),
                                "to_user": user,
                                "read": False,
                            }
                        )
                    )
                    await self.relay.mark_read(key, [message.id])
                    moved.add(message.id)
                    unmoved.remove(message)
            except BaseException:
                await self._restore(key, unmoved)
                raise

    async def _restore(self, key: str, unmoved: list[Message]) -> None:
        """Put *unmoved* back in the session inbox if the relay consumed them.

        A consuming relay no longer holds them after ``fetch``, so a probe
        fetch finds none of their ids and they are re-delivered, along with
        anything the probe itself consumed.  A non-consuming relay still
        holds them and the probe finds their ids, so nothing is re-delivered.

        Restored copies get ids derived from the originals, so repeating the
        restore cannot duplicate them.  One message can still be duplicated:
        the one whose mailbox delivery raised after the relay accepted it (a
        lost acknowledgement) is restored too.  A copy is the safe side of
        that ambiguity; losing mail is not.
        """
        probe = await self.relay.fetch(key)
        held = {m.id for m in probe}
        if any(m.id in held for m in unmoved):
            return
        failed: list[uuid.UUID] = []
        for message in [*unmoved, *probe]:
            try:
                await self.relay.deliver(
                    message.model_copy(update={"id": _restored_id(message.id)})
                )
            except Exception:
                failed.append(message.id)
                logger.error(
                    "could not restore message %s to %s", message.id, key, exc_info=True
                )
        if failed:
            logger.error("%d message(s) lost from %s; see the log", len(failed), key)

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
