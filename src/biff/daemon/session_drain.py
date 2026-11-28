"""Move a CLI session's unread mail into its owner's user mailbox."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from biff.daemon.mail_transfer import MailTransfer
from biff.models import Message
from biff.relay import Relay

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InboxDrain:
    """Re-files a session inbox into the user mailbox through one relay."""

    relay: Relay

    async def move(self, key: str, user: str) -> None:
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
            try:
                for message in batch:
                    await MailTransfer(self.relay).forward(key, user, message)
                    moved.add(message.id)
            except BaseException:
                await self._restore(key, batch, moved)
                raise

    async def _restore(
        self, key: str, batch: list[Message], moved: set[uuid.UUID]
    ) -> None:
        """Put the mail of *batch* not yet *moved*, and the probe consumed, back.

        A consuming relay no longer holds a batch message after ``fetch``,
        unless its ack was never confirmed and it is still queued.  A probe
        fetch finds exactly those, and consumes them with whatever else is
        queued, so what goes back is the batch messages not moved, plus
        everything the probe consumed.  A relay that does not consume still
        holds all of it, and nothing is re-delivered.

        A message already in *moved* that the probe found (its ack was not
        confirmed after the forward) is in the mailbox already.  Filing it
        again under a new id would let a retry forward it under a new mailbox id,
        a second copy, so it is dropped.

        Restored copies get ids derived from the originals, so repeating the
        restore cannot duplicate them.  One message can still be duplicated:
        the one whose mailbox delivery raised after the relay accepted it (a
        lost acknowledgement) is restored too.  A copy is the safe side of
        that ambiguity; losing mail is not.
        """
        if not self.relay.consumes_on_fetch:
            return
        probe = await self.relay.fetch(key)
        # By id: a batch message the probe found queued is one message.
        restore = {m.id: m for m in [*batch, *probe] if m.id not in moved}
        if lost := [m for m in restore.values() if not await self._refile(m)]:
            logger.error("Lost %d (tty=%s)", len(lost), key.partition(":")[2])

    async def _refile(self, message: Message) -> bool:
        """File a copy of *message* in the inbox again; say whether it was filed."""
        try:
            await self.relay.deliver(message.refiled())
        except Exception:
            logger.exception("Could not restore message %s", message.id)
            return False
        return True
