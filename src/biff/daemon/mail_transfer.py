"""File one message of a session inbox in its user's mailbox, once however often."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from biff.models import Message
    from biff.relay import Relay

__all__ = ["MailTransfer"]

_TRANSFER_NS = uuid.UUID("5d0c3f0e-8f4e-4f43-9a6f-0d2d6f1b7e11")


@dataclass(frozen=True, slots=True)
class MailTransfer:
    """Moves messages from session inboxes to user mailboxes through one relay."""

    relay: Relay

    async def forward(self, key: str, user: str, message: Message) -> None:
        """File *message*, from the inbox *key*, in *user*'s mailbox, then mark it read.

        The copy's id is derived from the message's, so doing this twice, after a
        failure that left the first attempt unsure, makes one copy.
        """
        copy = message.model_copy(
            update={
                "id": uuid.uuid5(_TRANSFER_NS, str(message.id)),
                "to_user": user,
                "read": False,
            }
        )
        await self.relay.deliver(copy)
        await self.relay.mark_read(key, [message.id])
