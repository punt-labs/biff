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
            unmoved = list(batch)
            try:
                for message in batch:
                    await MailTransfer(self.relay).forward(key, user, message)
                    moved.add(message.id)
                    unmoved.remove(message)
            except BaseException:
                await self._restore(key, unmoved)
                raise

    async def _restore(self, key: str, unmoved: list[Message]) -> None:
        """Put the mail *unmoved* left, and the probe consumed, back in the inbox.

        A consuming relay no longer holds an unmoved message after ``fetch``,
        unless its ack was never confirmed and it is still queued.  A probe
        fetch finds exactly those, and consumes them with whatever else is
        queued, so what goes back is the unmoved messages the probe did not
        find, plus everything it consumed.  A relay that does not consume still
        holds all of it, and nothing is re-delivered.

        Restored copies get ids derived from the originals, so repeating the
        restore cannot duplicate them.  One message can still be duplicated:
        the one whose mailbox delivery raised after the relay accepted it (a
        lost acknowledgement) is restored too.  A copy is the safe side of
        that ambiguity; losing mail is not.
        """
        if not self.relay.consumes_on_fetch:
            return
        probe = await self.relay.fetch(key)
        failed: list[uuid.UUID] = []
        # By id: an unmoved message the probe found queued is one message.
        for message in {m.id: m for m in [*unmoved, *probe]}.values():
            try:
                await self.relay.deliver(message.refiled())
            except Exception:
                failed.append(message.id)
                logger.exception("Could not restore message %s", message.id)
        if failed:
            logger.error("Lost %d (tty=%s)", len(failed), key.partition(":")[2])
