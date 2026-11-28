"""Build again, at daemon start, the harness sessions the last run held."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self, final

from biff.daemon.resolver import ResolveError
from biff.daemon.session_key import SessionKey
from biff.daemon.tasks import Supervised
from biff.relay_errors import RELAY_ERRORS
from biff.session_id import CLAUDE_CODE_ROLE
from biff.session_routing import SessionRouting

if TYPE_CHECKING:
    from biff.daemon.attachment import Attachment
    from biff.daemon.connection import DaemonCore
    from biff.daemon.harness_records import HarnessRecords, KeptSession
    from biff.frame_client import Hello

__all__ = ["SessionRestore"]

logger = logging.getLogger(__name__)


@final
class SessionRestore:
    """Gives a restarted daemon the sessions it held, so none of them is forgotten.

    Each kept session is opened as a client that connected and left, which is
    what a session with no connection is: retention, an end, and the mail waiting
    in its relay inbox then reach it as for any session, and a hello reattaches
    it.  Its retention clock resumes from when its last connection left.
    """

    __slots__ = ("_core", "_records", "_retry")

    RETRY_DELAYS: ClassVar[tuple[float, ...]] = (5.0, 15.0, 45.0)
    """Seconds to wait before each further try while the daemon runs."""
    MAX_FAILED_STARTS: ClassVar[int] = 5
    """Starts in a row that may fail to build a session before its record is dropped."""

    _core: DaemonCore
    _records: HarnessRecords
    _retry: asyncio.Task[None] | None

    def __new__(cls, records: HarnessRecords, core: DaemonCore) -> Self:
        """Restore the sessions *records* keeps into *core*'s table."""
        self = super().__new__(cls)
        self._records = records
        self._core = core
        self._retry = None
        return self

    async def run(self) -> None:
        """Restore every kept session, then keep trying those that failed.

        A record that cannot be restored never stops the start: it is counted, and
        tried again after each delay in :attr:`RETRY_DELAYS` and at the next start,
        until :attr:`MAX_FAILED_STARTS` starts in a row have failed.  A record
        whose repository is gone is dropped at once.
        """
        kept = self._records.load()
        failed = [k for k in kept if not await self._restore(k)]
        logger.info(
            "restored %d of %d kept sessions, %d failed",
            len(kept) - len(failed),
            len(kept),
            len(failed),
        )
        failed = [k for k in failed if not self._count_failure(k)]
        if failed:
            self._retry = Supervised.spawn(self._retry_later(failed), "session restore")

    async def stop(self) -> None:
        """Stop trying the sessions that have not come back."""
        retry, self._retry = self._retry, None
        if retry is not None:
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)

    async def _retry_later(self, failed: list[KeptSession]) -> None:
        for delay in self.RETRY_DELAYS:
            await asyncio.sleep(delay)
            failed = [k for k in failed if not await self._restore(k)]
            if not failed:
                return
        for kept in failed:
            logger.error(
                "session %s could not be restored while the daemon ran; its record "
                "is kept for the next start",
                kept.hello.session_id,
            )

    async def _restore(self, kept: KeptSession) -> bool:
        """Open the session dormant; ``False``: try again, ``True``: done or dropped."""
        hello = kept.hello
        if SessionKey.of(hello) in self._core.sessions.snapshot():
            return True  # a hello came first: its record is newer than this one
        try:
            async with asyncio.timeout(self._core.attach_timeout):
                attachment = await self._open(hello)
        except ResolveError:
            if not Path(hello.cwd).is_dir():
                self._drop(kept, "its repository is gone")
                return True
            logger.exception(
                "could not restore session %s: the configuration under %s is "
                "unusable; fix it and the next try restores the session",
                hello.session_id,
                hello.cwd,
            )
            return False
        except (TimeoutError, *RELAY_ERRORS):
            logger.warning(
                "could not restore session %s", hello.session_id, exc_info=True
            )
            return False
        except Exception:
            logger.exception("could not restore session %s", hello.session_id)
            return False
        if attachment is None:
            self._drop(kept, "its relay row is gone: it ended, or aged out while down")
            return True
        since = kept.dormant_since or datetime.now(UTC)
        attachment.detach()
        attachment.session.rest_for((datetime.now(UTC) - since).total_seconds())
        self._records.mark(SessionKey(hello.session_id, CLAUDE_CODE_ROLE), since)
        return True

    async def _open(self, hello: Hello) -> Attachment | None:
        """Open the session of *hello*; ``None``: the relay no longer lists it.

        A session that ended leaves no row, so a record that outlived its end (the
        delete failed) is not brought back, and neither is one whose row aged out.
        """
        core = self._core
        resolved = await core.resolver.resolve(hello)
        routing = SessionRouting(hello.session_id, role=SessionKey.of(hello).role)
        relay = core.pool.acquire(resolved)
        if await relay.get_session(routing.session_key(resolved.config.user)) is None:
            return None
        return await core.sessions.open(hello, resolved, core.pool)

    def _count_failure(self, kept: KeptSession) -> bool:
        """Count a failed start; drop the record, and say so, once too many failed."""
        key = SessionKey(kept.hello.session_id, CLAUDE_CODE_ROLE)
        if self._records.failed(key) < self.MAX_FAILED_STARTS:
            return False
        self._drop(
            kept, f"{self.MAX_FAILED_STARTS} starts in a row failed to restore it"
        )
        return True

    def _drop(self, kept: KeptSession, why: str) -> None:
        """Forget the record.

        The mail waiting in the session's relay inbox stays there: forwarding it
        needs the relay, and *why* is that the relay cannot be reached from the
        record.
        """
        session_id = kept.hello.session_id
        logger.error("dropping the record of session %s: %s", session_id, why)
        self._records.remove(SessionKey(session_id, CLAUDE_CODE_ROLE))
