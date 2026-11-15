"""Move a CLI session's unread mail into its owner's user mailbox."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from biff.models import Message
from biff.relay import Relay

logger = logging.getLogger(__name__)

_TRANSFER_NS = uuid.UUID("5d0c3f0e-8f4e-4f43-9a6f-0d2d6f1b7e11")
_RESTORE_NS = uuid.UUID("b3a1c7d2-2e49-4c52-8d1a-6e0f9c5a4b72")


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
                    await self.relay.deliver(
                        message.model_copy(
                            update={
                                "id": self._transfer_id(message.id),
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
                    message.model_copy(update={"id": self._restored_id(message.id)})
                )
            except Exception:
                failed.append(message.id)
                logger.error(
                    "could not restore message %s to %s", message.id, key, exc_info=True
                )
        if failed:
            logger.error("%d message(s) lost from %s; see the log", len(failed), key)

    @staticmethod
    def _transfer_id(source: uuid.UUID) -> uuid.UUID:
        """Stable id of *source*'s copy in the user mailbox."""
        return uuid.uuid5(_TRANSFER_NS, str(source))

    @staticmethod
    def _restored_id(source: uuid.UUID) -> uuid.UUID:
        """Stable id of *source*'s copy put back in the session inbox."""
        return uuid.uuid5(_RESTORE_NS, str(source))
