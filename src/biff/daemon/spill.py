"""Mail a stopping daemon could not give back, kept on disk until it is filed."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING, ClassVar, Self, cast, final

from biff.commands._result import CommandResult
from biff.daemon.mail_transfer import MailTransfer
from biff.daemon.spill_entry import Held, Spilled, SpillKey, Tries
from biff.daemon.spill_file import SpillFile
from biff.relay_errors import RELAY_ERRORS
from biff.relay_seed import RelayConflictError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from biff.daemon.connection import DaemonCore
    from biff.daemon.sessions import Session
    from biff.models import Message
    from biff.relay import Relay

__all__ = ["Spill"]

logger = logging.getLogger(__name__)

_STATUS_LINES = {
    "spilled_messages": "spilled: {} messages pending to be filed",
    "undeliverable_messages": "undeliverable: {} spilled messages are shelved",
    "spill_files_set_aside": "set aside: {} spill files hold mail that needs a person",
}


@final
class Spill:
    """The mail a stopping daemon could not file back, which a running one files.

    A consuming relay removed that mail at fetch, so while it is only in memory a
    stop that cannot file it in time (the budget ran out, the relay refused, a
    fetch finished after the hand-back began) would lose it.  The spill file is
    then the only copy: nothing here raises over it, and each failure says what
    is at stake.  The stop writes it first and removes what it filed; the next
    start files the rest back, and a running daemon keeps trying.
    """

    __slots__ = ("_file", "_pending", "_unsaved", "_unshelved_at")

    REFILE_SECONDS: ClassVar[float] = 15.0
    """The most all sessions' refiling may take in one pass."""
    RETRY_DELAYS: ClassVar[tuple[float, ...]] = (5.0, 15.0, 45.0)
    """Waits before the first tries after start, as a network that is not up yet."""
    RETRY_INTERVAL: ClassVar[float] = 300.0
    """Seconds between tries once those are used."""
    UNSHELVE_INTERVAL: ClassVar[float] = 86400.0
    """How often a running daemon puts the shelf back in the spill to try it again."""
    UNDELIVERABLE_AFTER: ClassVar[timedelta] = timedelta(days=7)
    UNDELIVERABLE_ATTEMPTS: ClassVar[int] = 20
    """Mail goes to the shelf only after this many failed tries, spread over this
    much time the daemon spent failing (time it was not running does not count)."""

    _file: SpillFile
    _pending: dict[SpillKey, Spilled] | None
    _unsaved: bool
    _unshelved_at: float

    def __new__(cls, path: Path, fallback: Path) -> Self:
        """Keep the spill in *path*, and in *fallback* if *path* cannot be written."""
        self = super().__new__(cls)
        self._file = SpillFile(path, fallback)
        self._pending = None
        self._unsaved = False
        self._unshelved_at = time.monotonic()
        return self

    def pending(self) -> int:
        """Return how many spilled messages wait to be filed back."""
        return sum(len(item.held) for item in self._load().values())

    def undeliverable(self) -> int:
        """Return how many spilled messages are shelved as undeliverable."""
        return self._file.shelved()

    def set_aside(self) -> int:
        """Return how many spill files were set aside as unreadable."""
        return len(self._file.asides())

    def annotate(self, status: CommandResult) -> CommandResult:
        """Return the status *status* with the spilled mail it should show.

        No spilled mail, no line: ``biff status`` stays as it was.
        """
        shown = {
            "spilled_messages": self.pending(),
            "undeliverable_messages": self.undeliverable(),
            "spill_files_set_aside": self.set_aside(),
        }
        counts = {name: n for name, n in shown.items() if n}
        if not counts:
            return status
        lines = [_STATUS_LINES[name].format(n) for name, n in counts.items()]
        data = status.json_data
        merged: object = data
        if isinstance(data, dict):
            merged = {**cast("dict[str, object]", data), **counts}
        return CommandResult(
            text="\n".join([status.text, *lines]),
            json_data=merged,
            error=status.error,
        )

    def hold(self, session: Session, messages: Sequence[Message]) -> bool:
        """Keep *messages*, held for *session*, on disk; say whether the file has them.

        Nothing here raises: this runs inside a read whose relay already gave the
        mail up, and the mail would be nowhere.  A batch that cannot be written
        stays in memory and is written with the next one, or by the next try at
        filing; one that cannot even be built is logged whole.
        """
        if not messages:
            return True
        try:
            merged = dict(self._load())
            key = (session.session_id, session.is_agent)
            config = session.context.config
            spilled = merged.get(key) or Spilled(
                session_id=session.session_id,
                agent=session.is_agent,
                cwd=session.cwd,
                user=session.context.user,
                session_key=session.context.session_key,
                relay=Spilled.relay_id(config.relay_url, config.repo_name),
                held=(),
            )
            merged[key] = spilled.with_mail(messages)
            if self._store(merged):
                return True
        except Exception:
            logger.exception(
                "could not build the spill for session %s", session.session_id
            )
        logger.critical(
            "LOST unless found here: %d messages of session %s could not be "
            "spilled (%d already spilled): %s",
            len(messages),
            session.session_id,
            self.pending(),
            "; ".join(self._whole(m) for m in messages),
        )
        return False

    @staticmethod
    def _whole(message: Message) -> str:
        """Return *message* as JSON for the log, or its repr if it will not dump."""
        try:
            return message.model_dump_json()
        except ValueError:
            return repr(message)

    def release(self, session: Session, filed: Sequence[Message]) -> None:
        """Forget *filed*, which was given back to the relay."""
        key = (session.session_id, session.is_agent)
        item = self._load().get(key)
        if item is None or not filed:
            return
        merged = dict(self._load())
        rest = item.without(filed)
        if rest.held:
            merged[key] = rest
        else:
            del merged[key]
        if not self._store(merged):
            logger.error(
                "could not record that %d messages of session %s were filed; "
                "they may be filed again",
                len(filed),
                session.session_id,
            )

    async def resume(self, core: DaemonCore) -> None:
        """File the spill back at start: say what needs a person, retry the shelf.

        Mail on the shelf and in files set aside is reported at every start; the
        shelf is tried again, so mail that failed for a bad week is not forgotten.
        """
        self._load()  # reading is what sets an unreadable file aside
        if asides := self._file.asides():
            logger.error(
                "%d spill files were set aside and hold mail that needs a person: %s",
                len(asides),
                ", ".join(str(p) for p in asides),
            )
        self._unshelve()
        await self.refile(core)

    async def refile(self, core: DaemonCore) -> None:
        """File every spilled message back to its relay, within the total budget.

        A session whose mail cannot be filed now (the relay is down, the
        repository is gone) stays in the file with each message's failed tries
        counted; one that failed often enough over long enough goes to the shelf
        beside it.  Delivery is at least once: a refiled copy carries an id derived
        from the original's, which the relay drops as a duplicate only inside its
        duplicate window, so a refile that landed but was reported failed can be
        delivered twice by a later try.
        """
        pending = dict(self._load())
        if not pending:
            return
        logger.warning(
            "%d spilled messages pending for %d sessions",
            self.pending(),
            len(pending),
        )
        try:
            async with asyncio.timeout(self.REFILE_SECONDS):
                for key, item in tuple(pending.items()):
                    await self._file_session(core, pending, key, item)
        except TimeoutError:
            logger.error(
                "filing the spill ran out of its %g s; the rest stays in the file",
                self.REFILE_SECONDS,
            )

    async def keep_filing(self, core: DaemonCore) -> None:
        """Try again for as long as the daemon runs, after the first tries at start.

        A daemon started at login may find no network for a while: the mail waits
        a few seconds, then minutes, not for the next restart.
        """
        for delay in self.RETRY_DELAYS:
            await asyncio.sleep(delay)
            await self._retry(core)
        while True:
            await asyncio.sleep(self.RETRY_INTERVAL)
            await self._retry(core)

    async def _retry(self, core: DaemonCore) -> None:
        """Write what could not be written, then file what is spilled.

        The shelf is put back in the spill once a day, so mail shelved while the
        daemon runs is tried again without waiting for a restart.
        """
        if self._unsaved:
            self._store(self._load())
        if time.monotonic() - self._unshelved_at >= self.UNSHELVE_INTERVAL:
            self._unshelve()
        await self.refile(core)

    def _unshelve(self) -> None:
        """Put the shelf back in the spill, as new mail, and drop the shelf."""
        self._unshelved_at = time.monotonic()
        shelved = self._file.take_shelf()
        if not shelved:
            return
        logger.error(
            "%d spilled messages were shelved as undeliverable; trying them again",
            sum(len(item.held) for item in shelved),
        )
        merged = dict(self._load())
        for item in shelved:
            fresh = item.keeping([h.renewed() for h in item.held])
            earlier = merged.get(item.key)
            merged[item.key] = fresh if earlier is None else earlier.merged_with(fresh)
        if self._store(merged):
            self._file.clear_shelf()

    def _store(self, items: Mapping[SpillKey, Spilled]) -> bool:
        """Make *items* the spill, in memory first; say whether the file has it."""
        self._pending = dict(items)
        self._unsaved = True
        try:
            self._file.write(items)
        except (OSError, TypeError, ValueError):
            logger.exception("could not write the spill")
            return False
        self._unsaved = False
        return True

    async def _file_session(
        self,
        core: DaemonCore,
        pending: dict[SpillKey, Spilled],
        key: SpillKey,
        item: Spilled,
    ) -> None:
        """File *item* back; what was filed is recorded even if the budget cuts in."""
        tries = Tries()
        try:
            await self._file_back(core, item, tries)
        finally:
            rest = self._remaining(item, tries)
            if rest is None:
                del pending[key]
            else:
                pending[key] = rest
            if not self._store(pending):
                logger.error("could not record the progress of filing the spill")

    async def _file_back(self, core: DaemonCore, item: Spilled, tries: Tries) -> None:
        """File *item*'s messages, noting in *tries* what was tried and what went."""
        try:
            async with asyncio.timeout(core.attach_timeout):
                resolved = await core.resolver.resolve(item.hello())
                now = Spilled.relay_id(
                    resolved.config.relay_url, resolved.config.repo_name
                )
                if item.relay and item.relay != now:
                    tries.session_failed = True
                    logger.error(
                        "the mail of session %s came from relay %s, which is not the "
                        "one the repository names now (%s): kept, not filed",
                        item.session_id,
                        item.relay,
                        now,
                    )
                else:
                    await self._deliver(core.pool.acquire(resolved), item, tries)
        except RelayConflictError as exc:
            logger.warning(
                "the spilled mail of %s stays unfiled: %s", item.session_id, exc
            )
        except Exception:
            # Before any message was tried the whole session failed; after, the
            # ones not yet tried were not at fault.
            tries.session_failed = tries.session_failed or not tries.tried
            logger.exception("could not file the spilled mail of %s", item.session_id)

    def _remaining(self, item: Spilled, tries: Tries) -> Spilled | None:
        """Return what of *item* is not filed, tries counted; ``None``: nothing.

        A try counts against a message only if it was tried, or its whole session
        could not be reached.
        """
        left = tuple(
            h.failed() if tries.session_failed or h.message.id in tries.tried else h
            for h in item.held
            if h.message.id not in tries.filed
        )
        left = self._shelve_old(item, left)
        if not left:
            return None
        logger.error(
            "%d spilled messages of session %s are still unfiled (oldest %s)",
            len(left),
            item.session_id,
            max(h.age() for h in left),
        )
        return item.keeping(left)

    def _shelve_old(self, item: Spilled, left: tuple[Held, ...]) -> tuple[Held, ...]:
        """Move what has failed often enough, for long enough, to the shelf.

        How long is the time the daemon spent failing, not the time since the mail
        was spilled: days with the daemon off are not days of failing.
        """
        old = [
            h
            for h in left
            if h.attempts >= self.UNDELIVERABLE_ATTEMPTS
            and h.failing() > self.UNDELIVERABLE_AFTER
        ]
        if not old or not self._file.shelve(item.keeping(old)):
            return left
        logger.error(
            "%d spilled messages of session %s failed %d times over %s: shelved "
            "as undeliverable",
            len(old),
            item.session_id,
            max(h.attempts for h in old),
            max(h.failing() for h in old),
        )
        return tuple(h for h in left if h not in old)

    @staticmethod
    async def _deliver(relay: Relay, item: Spilled, tries: Tries) -> None:
        """File each message of *item* with *relay*, noting what was tried and went.

        A session the relay no longer lists has ended: its mail goes to its user's
        mailbox, as an end would have sent it, not to an inbox nobody reads.  The
        copy's id is derived from the message's, and the relay drops a repeat of
        it only inside its duplicate window, so a try made again later after one
        that landed unseen can deliver the message twice: at least once.

        One message that fails, for any reason, does not stop the ones after it.
        """
        key = item.session_key
        ended = bool(key) and await relay.get_session(key) is None
        for message in item.messages:
            tries.tried.add(message.id)
            try:
                if ended:
                    await MailTransfer(relay).forward(key, item.user, message)
                else:
                    await relay.deliver(message.refiled())
            except RELAY_ERRORS:
                logger.exception("could not file spilled %s", message.id)
            except Exception:
                logger.exception(
                    "spilled %s could not be filed (not a relay error)", message.id
                )
            else:
                tries.filed.add(message.id)

    def _load(self) -> dict[SpillKey, Spilled]:
        """Return what the files hold, read once."""
        if self._pending is None:
            self._pending = self._file.read()
        return self._pending
