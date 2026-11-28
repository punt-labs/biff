"""Mail a stopping daemon could not give back, kept on disk for the next start."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, ClassVar, Self, cast, final

from biff.commands._result import CommandResult
from biff.daemon.mail_transfer import MailTransfer
from biff.daemon.spill_file import Held, Spilled, SpillFile, SpillKey
from biff.relay_errors import RELAY_ERRORS

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from biff.daemon.connection import DaemonCore
    from biff.daemon.sessions import Session
    from biff.models import Message
    from biff.relay import Relay

__all__ = ["Spill"]

logger = logging.getLogger(__name__)


@final
class Spill:
    """The mail a stopping daemon could not file back, which the next start files.

    A consuming relay removed that mail at fetch, so while it is only in memory a
    stop that cannot file it in time (the budget ran out, the relay refused, a
    fetch finished after the hand-back began) would lose it.  The spill file is
    then the only copy: nothing here raises over it, and each failure says what
    is at stake.
    """

    __slots__ = ("_file", "_pending")

    REFILE_SECONDS: ClassVar[float] = 15.0
    """The most all sessions' refiling may take at start, before the port binds."""
    UNDELIVERABLE_AFTER: ClassVar[timedelta] = timedelta(days=7)
    """How long mail may fail to be filed before it goes to the shelf."""

    _file: SpillFile
    _pending: dict[SpillKey, Spilled] | None

    def __new__(cls, path: Path, fallback: Path) -> Self:
        """Keep the spill in *path*, and in *fallback* if *path* cannot be written."""
        self = super().__new__(cls)
        self._file = SpillFile(path, fallback)
        self._pending = None
        return self

    def pending(self) -> int:
        """Return how many spilled messages wait to be filed back."""
        return sum(len(item.held) for item in self._load().values())

    def undeliverable(self) -> int:
        """Return how many spilled messages are shelved as undeliverable."""
        return self._file.shelved()

    def annotate(self, status: CommandResult) -> CommandResult:
        """Return the status *status* with the spilled mail it should show.

        No spilled mail, no line: ``biff status`` stays as it was.
        """
        shown = {
            "spilled_messages": sum(len(i.held) for i in self._load().values()),
            "undeliverable_messages": self._file.shelved(),
        }
        counts = {name: n for name, n in shown.items() if n}
        if not counts:
            return status
        lines = [
            f"spilled: {n} messages pending to be filed"
            if name == "spilled_messages"
            else f"undeliverable: {n} spilled messages are shelved"
            for name, n in counts.items()
        ]
        data = status.json_data
        merged: object = data
        if isinstance(data, dict):
            merged = {**cast("dict[str, object]", data), **counts}
        return CommandResult(
            text="\n".join([status.text, *lines]),
            json_data=merged,
            error=status.error,
        )

    def hold(self, session: Session, messages: Sequence[Message]) -> None:
        """Keep *messages*, held for *session*, on disk; never raises over a write."""
        if not messages:
            return
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
        try:
            self._file.write(merged)
        except OSError:
            logger.critical(
                "LOST unless found here: %d messages of session %s could not be "
                "spilled (%d already spilled): %s",
                len(messages),
                session.session_id,
                self.pending(),
                ", ".join(str(m.id) for m in messages),
                exc_info=True,
            )
            return
        self._pending = merged

    async def refile(self, core: DaemonCore) -> None:
        """File every spilled message back to its relay, within the total budget.

        A session whose mail cannot be filed now (the relay is down, the
        repository is gone) stays in the file, and one that has failed for longer
        than :attr:`UNDELIVERABLE_AFTER` goes to the shelf beside it.  Refiled
        copies carry derived ids, so a message filed twice is one.
        """
        if shelved := self._file.shelved():
            logger.error("%d spilled messages are shelved as undeliverable", shelved)
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
                    rest = await self._file_back(core, item)
                    self._settle(pending, key, rest)
        except TimeoutError:
            logger.error(
                "filing the spill ran out of its %g s; the rest stays in the file",
                self.REFILE_SECONDS,
            )

    def _settle(
        self, pending: dict[SpillKey, Spilled], key: SpillKey, rest: Spilled | None
    ) -> None:
        """Record that *key*'s mail is now *rest*, in memory and in the file."""
        if rest is None:
            del pending[key]
        else:
            pending[key] = rest
        try:
            self._file.write(pending)
        except OSError:
            logger.exception("could not record the progress of filing the spill")
        self._pending = dict(pending)

    async def _file_back(self, core: DaemonCore, item: Spilled) -> Spilled | None:
        """File *item* back; return what was not (``None``: nothing is left)."""
        filed = set[object]()
        try:
            async with asyncio.timeout(core.attach_timeout):
                resolved = await core.resolver.resolve(item.hello())
                now = Spilled.relay_id(
                    resolved.config.relay_url, resolved.config.repo_name
                )
                if item.relay and item.relay != now:
                    logger.error(
                        "the mail of session %s came from relay %s, which is not the "
                        "one the repository names now (%s): kept, not filed",
                        item.session_id,
                        item.relay,
                        now,
                    )
                else:
                    await self._deliver(core.pool.acquire(resolved), item, filed)
        except Exception:
            logger.exception("could not file the spilled mail of %s", item.session_id)
        left = tuple(h for h in item.held if h.message.id not in filed)
        return self._keep(item, self._shelve_old(item, left))

    def _shelve_old(self, item: Spilled, left: tuple[Held, ...]) -> tuple[Held, ...]:
        """Move what has failed past :attr:`UNDELIVERABLE_AFTER` to the shelf."""
        old = [h for h in left if h.age() > self.UNDELIVERABLE_AFTER]
        if not old or not self._file.shelve(item.keeping(old)):
            return left
        logger.error(
            "%d spilled messages of session %s failed for %s: shelved as undeliverable",
            len(old),
            item.session_id,
            max(h.age() for h in old),
        )
        return tuple(h for h in left if h not in old)

    @staticmethod
    def _keep(item: Spilled, left: tuple[Held, ...]) -> Spilled | None:
        """Return *item* holding *left*, or ``None`` when nothing is left."""
        if not left:
            return None
        logger.error(
            "%d spilled messages of session %s are still unfiled (oldest %s)",
            len(left),
            item.session_id,
            max(h.age() for h in left),
        )
        return item.keeping(left)

    @staticmethod
    async def _deliver(relay: Relay, item: Spilled, filed: set[object]) -> None:
        """File each message of *item* with *relay*, noting the ones that went.

        A session the relay no longer lists has ended: its mail goes to its user's
        mailbox, as an end would have sent it, not to an inbox nobody reads.
        """
        key = item.session_key
        ended = bool(key) and await relay.get_session(key) is None
        for message in item.messages:
            try:
                if ended:
                    await MailTransfer(relay).forward(key, item.user, message)
                else:
                    await relay.deliver(message.refiled())
            except RELAY_ERRORS:
                logger.exception("could not file spilled %s", message.id)
            else:
                filed.add(message.id)

    def _load(self) -> dict[SpillKey, Spilled]:
        """Return what the files hold, read once."""
        if self._pending is None:
            self._pending = self._file.read()
        return self._pending
