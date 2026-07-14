"""Value and enum types for the talk state machine (talk.tex schemas).

These are the pure, dependency-free data objects the ``TalkState`` machine
consumes and produces: the phase and accept-outcome enums, the typed
notification frame, a pending invite, and the agent-mode drain snapshot.
Isolating them here (PY-IC-9) lets front-ends and tests import the talk
vocabulary without pulling in the behavioural machinery or its relay
dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Mapping


class TalkPhase(Enum):
    """The three phases of a talk session from one side's perspective."""

    IDLE = auto()  # tpIdle — normal mode, not in talk
    INVITING = auto()  # tpInviting — sent an invite, waiting for accept
    CONNECTED = auto()  # tpConnected — exchanging messages


class AcceptOutcome(Enum):
    """Result of polling for an accept while inviting (talk.tex handshake)."""

    NONE = auto()  # nothing yet — keep waiting
    ACCEPTED = auto()  # the partner accepted our invite
    AUTO_ACCEPT = auto()  # mutual invite; we are the higher key and auto-accept


@dataclass(frozen=True, slots=True)
class PendingInvite:
    """A pending talk invite awaiting a response (notification.tex ``talkPending``).

    Retains the inviter's session key so the accept hint names a specific
    session (``talk @user:tty``, never a bare ``@user`` that fails at the
    prompt), and the monotonic arrival time so the poller can age out an
    invite whose inviter never returns (notification.tex ``ExpirePendingInvite``).
    """

    user: str
    session_key: str
    arrived: float

    @property
    def tty(self) -> str:
        """The inviter's tty, parsed from the ``user:tty`` session key."""
        _, _, tty = self.session_key.partition(":")
        return tty

    @property
    def accept_command(self) -> str:
        """A runnable command that accepts this invite by naming the session."""
        return f"talk @{self.session_key}"


@dataclass(frozen=True, slots=True)
class TalkNotification:
    """A typed talk notification (talk.tex ``Notification`` schema)."""

    ntype: str
    nfrom: str
    nfrom_tty: str
    nfrom_key: str
    nto: str
    nbody: str

    @classmethod
    def from_payload(cls, raw: Mapping[str, object]) -> Self:
        """Build a notification from a raw NATS JSON payload.

        Unknown ``type`` defaults to ``message`` — matching the REPL's
        historical drain behaviour where any non-control frame renders
        as a conversation line.
        """
        return cls(
            ntype=str(raw.get("type", "message")),
            nfrom=str(raw.get("from", "?")),
            nfrom_tty=str(raw.get("from_tty", "")),
            nfrom_key=str(raw.get("from_key", "")),
            nto=str(raw.get("to_key", "")),
            nbody=str(raw.get("body", "")),
        )

    @property
    def is_invite(self) -> bool:
        """Whether this is an ``invite`` control frame."""
        return self.ntype == "invite"

    @property
    def is_accept(self) -> bool:
        """Whether this is an ``accept`` control frame."""
        return self.ntype == "accept"

    @property
    def is_end(self) -> bool:
        """Whether this is an ``end`` control frame."""
        return self.ntype == "end"

    @property
    def is_withdraw(self) -> bool:
        """Whether this is a ``withdraw`` control frame (ntWithdraw)."""
        return self.ntype == "withdraw"


@dataclass(frozen=True, slots=True)
class AgentDrain:
    """Result of an MCP agent-mode drain (non-modal front-end).

    The MCP server is not modal like the REPL, so it drains the whole
    queue in one pass: invites are recorded, an accept from the invited
    session connects us, messages are surfaced, and an end resets to idle.
    """

    messages: tuple[TalkNotification, ...]
    pending: Mapping[str, PendingInvite]
    connected: bool
    ended: bool
