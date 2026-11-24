"""What a channel event says: fixed templates, no bodies, claimed senders (DES-087).

An event enters the model's context as a prompt, and anyone holding the relay
credential can publish a frame with any ``from``.  So an event carries a count
or a short claimed sender, never a body, and a talk invite tells the model to
ask the user.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from biff.session_routing import ID_RE

MAX_EVENT_LENGTH = 200
MAX_SENDER_USER_LENGTH = 39

# The actionable part first: the client shows one line and cuts the rest.
_WALL = "Call wall to read: new biff wall from {}."
_INVITE = "Ask the user: talk invite from {}."
_ANONYMOUS = "a teammate (claimed, unverified)"
_OWN_IDENTITY = "unverified (claims to be you)"


class EventKind(StrEnum):
    """The kinds of arrival a channel event announces; also the ``meta`` kind."""

    MAIL = "mail"
    WALL = "wall"
    TALK_INVITE = "talk_invite"
    TALK_MESSAGE = "talk_message"


@dataclass(frozen=True, slots=True)
class Sender:
    """A sender as the model may see it: claimed, unverified, short."""

    text: str

    @classmethod
    def claimed(cls, user: str, tty: str, *, own_user: str) -> Self:
        """Label a claimed ``user:tty``, or an anonymous teammate.

        The address is shown only when each part fully matches ``ID_RE`` and the
        user is at most 39 characters.  A claim to be the receiving session's own
        user is named as such rather than shown.
        """
        is_plain = (
            ID_RE.fullmatch(user) is not None
            and ID_RE.fullmatch(tty) is not None
            and len(user) <= MAX_SENDER_USER_LENGTH
        )
        if not is_plain:
            return cls(_ANONYMOUS)
        if user == own_user:
            return cls(_OWN_IDENTITY)
        return cls(f"{user}:{tty} (claimed, unverified)")

    @classmethod
    def anonymous(cls) -> Self:
        """Return the sender used when the address cannot be shown."""
        return cls(_ANONYMOUS)

    def __str__(self) -> str:
        """Return the label that goes into an event."""
        return self.text


@dataclass(frozen=True, slots=True)
class ChannelEvent:
    """One ``notifications/claude/channel`` payload: text and a closed ``meta``."""

    kind: EventKind
    content: str
    count: int | None = None

    @property
    def meta(self) -> dict[str, str]:
        """The closed ``meta`` set: ``kind``, and ``count`` for mail."""
        meta = {"kind": self.kind.value}
        if self.count is not None:
            meta["count"] = str(self.count)
        return meta

    @classmethod
    def mail(cls, count: int) -> Self:
        """Announce the *count* messages unread now (not the new ones); no sender."""
        noun = "message" if count == 1 else "messages"
        text = f"Call read_messages: {count} unread biff {noun}."
        return cls(EventKind.MAIL, text, count)

    @classmethod
    def wall(cls, sender: Sender) -> Self:
        """Announce a new wall from a claimed *sender*."""
        return cls(EventKind.WALL, _fit(_WALL, sender))

    @classmethod
    def talk_invite(cls, sender: Sender) -> Self:
        """Announce an invite to talk; the model is told to ask the user."""
        return cls(EventKind.TALK_INVITE, _fit(_INVITE, sender))

    @classmethod
    def talk_message(cls) -> Self:
        """Announce a message from the connected partner; names no sender."""
        text = "Call talk_read: new message from your talk partner."
        return cls(EventKind.TALK_MESSAGE, text)


def _fit(template: str, sender: Sender) -> str:
    """Fill *template* with *sender*, or with no address if that is too long."""
    text = template.format(sender)
    if len(text) <= MAX_EVENT_LENGTH:
        return text
    return template.format(Sender.anonymous())
