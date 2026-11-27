"""Lapse stale stored sessions to dormant and expire those past retention."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from biff.daemon.resolver import ResolveError
from biff.daemon.session_host import SessionHost
from biff.daemon.session_pointer import CurrentPointer
from biff.daemon.session_select import SessionSelector
from biff.frame_client import Hello
from biff.relay_errors import RELAY_ERRORS
from biff.tty import get_hostname

if TYPE_CHECKING:
    from collections.abc import Callable

    from biff.daemon.relays import RelayPool
    from biff.daemon.resolver import SessionResolver
    from biff.daemon.session_store import CliSessionRecord, CliSessionStore

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

    __slots__ = ("_attached", "_pool", "_resolver", "_store")

    _store: CliSessionStore
    _resolver: SessionResolver
    _pool: RelayPool
    _attached: Callable[[str], bool]

    def __new__(
        cls,
        store: CliSessionStore,
        resolver: SessionResolver,
        pool: RelayPool,
        attached: Callable[[str], bool],
    ) -> Self:
        """Sweep the records in *store*, reaching relays through *pool*.

        *attached* says whether a connection is attached to a session id.  Such
        a session is in use however long ago a command last renewed it, so the
        sweep renews it instead of judging it.
        """
        self = super().__new__(cls)
        self._store = store
        self._resolver = resolver
        self._pool = pool
        self._attached = attached
        return self

    async def sweep(self) -> None:
        """Judge every record of this host once.

        Housekeeping must never take the daemon down, so a record that cannot be
        judged is logged and the sweep moves on.
        """
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
                if self._attached(record.session_id) and record.state != "ended":
                    await self._renew(record)
                else:
                    await self._judge(record)
            except ResolveError as exc:
                logger.info("session %s left alone: %s", record.session_id, exc)
            except (OSError, ValueError, *RELAY_ERRORS):
                logger.warning(
                    "session sweep failed for %s", record.session_id, exc_info=True
                )

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
