"""Lapse stale stored sessions to dormant and expire those past retention."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from biff.daemon.held_mail import HeldMail
from biff.daemon.resolver import ResolveError
from biff.daemon.session_end import RETENTION_SECONDS, SessionEnd
from biff.daemon.session_host import SessionHost
from biff.daemon.session_pointer import CurrentPointer
from biff.daemon.session_select import SessionSelector
from biff.frame_client import Hello
from biff.relay_errors import RELAY_ERRORS
from biff.tty import get_hostname

if TYPE_CHECKING:
    from biff.daemon.relays import RelayPool
    from biff.daemon.resolver import SessionResolver
    from biff.daemon.session_store import CliSessionRecord, CliSessionStore
    from biff.daemon.sessions import SessionTable

__all__ = ["SessionSweeper"]

logger = logging.getLogger(__name__)

# The resolver reads the client, directory and user of a hello and nothing
# else; a sweep has no connection to prove, so the proof is all zeros.
_NO_PROOF = "0" * 64


@final
class SessionSweeper:
    """Judges every stored session of this host against the lease and retention.

    A session idle past its lease becomes dormant; a dormant or ended one past
    retention is removed and, unless it ended, its relay row and tty name are
    released.  Each record is judged on the relay its own repository selects, so
    one sweep covers every repository the host has used.
    """

    __slots__ = ("_completed", "_live", "_pool", "_resolver", "_store")

    _store: CliSessionStore
    _resolver: SessionResolver
    _pool: RelayPool
    _live: SessionTable
    _completed: int

    def __new__(
        cls,
        store: CliSessionStore,
        resolver: SessionResolver,
        pool: RelayPool,
        live: SessionTable,
    ) -> Self:
        """Sweep the records in *store*, reaching relays through *pool*.

        *live* holds the sessions connections are attached to.  A session with a
        connection is in use however long ago a command last renewed it, so the
        sweep renews it instead of judging it, and it waits for a read in flight
        on a session before judging that one.
        """
        self = super().__new__(cls)
        self._store = store
        self._resolver = resolver
        self._pool = pool
        self._live = live
        self._completed = 0
        return self

    @property
    def completed(self) -> int:
        """How many sweeps have finished; a test waits on it instead of on time."""
        return self._completed

    async def sweep(self) -> None:
        """Judge every record of this host once.

        Housekeeping must never take the daemon down, so a record that cannot be
        judged is logged and the sweep moves on.
        """
        try:
            await self._sweep_all()
            await self._expire_claude()
        finally:
            self._completed += 1

    async def _expire_claude(self) -> None:
        """End the harness sessions whose retention is spent."""
        await SessionEnd(self._live).expire(RETENTION_SECONDS)

    async def _sweep_all(self) -> None:
        try:
            records = self._store.list()
        except OSError:
            logger.warning("session sweep could not list sessions", exc_info=True)
            return
        hostname = get_hostname()
        for record in records:
            if record.hostname != hostname:
                continue
            try:
                await self._sweep_one(record)
            except ResolveError as exc:
                logger.info("session %s left alone: %s", record.session_id, exc)
            except (OSError, ValueError, *RELAY_ERRORS):
                logger.warning(
                    "session sweep failed for %s", record.session_id, exc_info=True
                )

    async def _sweep_one(self, record: CliSessionRecord) -> None:
        held = self._live.holding(record.session_id)
        if held is None:
            await self._judge(record)
            return
        async with held.exclusive():
            if not held.is_dormant and record.state != "ended":
                await self._renew(record)
                return
            await HeldMail.refile(held)
            await self._judge(record)
            try:
                self._store.read(record.session_id)
            except FileNotFoundError:  # the judgement expired it
                held.mark_ended()
                self._live.retire(record.session_id)

    async def _renew(self, record: CliSessionRecord) -> None:
        async with self._store.lifecycle(record.session_id):
            self._store.attach(record.session_id, now=datetime.now(UTC))

    async def _judge(self, record: CliSessionRecord) -> None:
        hello = Hello(
            mac=_NO_PROOF,
            client="cli",
            session_id=record.session_id,
            cwd=record.repo_root,
            user=record.user,
        )
        resolved = await self._resolver.resolve(hello)
        selector = SessionSelector(
            store=self._store,
            pointer=CurrentPointer(self._store.root.parent / "current"),
            user=record.user,
            repo=record.repo,
            repo_root=Path(record.repo_root),
            hostname=record.hostname,
        )
        host = SessionHost(
            relay=self._pool.acquire(resolved),
            config=resolved.config,
            selector=selector,
            hostname=record.hostname,
            pwd=record.repo_root,
        )
        await host.sweep_one(record.session_id)
