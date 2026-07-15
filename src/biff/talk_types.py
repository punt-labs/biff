"""Value and enum types for the talk state machine (talk.tex schemas).

These are the pure, dependency-free data objects the ``TalkState`` machine
consumes and produces: the phase and accept-outcome enums, the typed
notification frame, a pending invite, and the agent-mode drain snapshot.
Isolating them here (PY-IC-9) lets front-ends and tests import the talk
vocabulary without pulling in the behavioural machinery or its relay
dependency.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Mapping

_CONTROL_TYPES = frozenset({"invite", "accept", "end", "withdraw"})
"""The session-scoped control frame types (talk.tex control notifications)."""

_KNOWN_TYPES = _CONTROL_TYPES | {"message"}
"""Every modeled talk frame type — a frame typed outside this set is a wake poke."""


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
    prompt), the inviter's display tty name (``ttyN``) so the hint reads as
    the same address ``/who`` shows and ``talk @user:ttyN`` resolves against
    — not the opaque session-key hex — and the monotonic arrival time so the
    poller can age out an invite whose inviter never returns (notification.tex
    ``ExpirePendingInvite``).
    """

    user: str
    session_key: str
    tty: str
    arrived: float

    def __post_init__(self) -> None:
        """Enforce HintNamesSession: the key must name *this inviter's* session.

        A key missing either half — colonless (``user``), empty-tty (``user:``),
        or empty-user (``:tty``) — could only render a hint that fails at the
        prompt (``talk @user`` or ``talk @:tty``), so it is rejected at
        construction to keep every recorded invite's hint runnable
        (notification.tex ``HintNamesSession``).

        The key's user half must also equal ``user`` — the frame's ``from``.
        The accept path derives its target tty from ``session_key`` while the
        pending set is keyed by ``user``, so a frame whose ``from`` and
        ``from_key`` name different users (``user=A``, ``session_key=B:tty``)
        would let an accept addressed to ``A`` connect to ``B`` instead.  A
        mismatch is a forged or corrupt frame and is rejected here.

        This is the single validation gate — the wire boundary constructs
        through :meth:`from_notification` and inherits it.
        """
        key_user, sep, tty = self.session_key.partition(":")
        if not key_user or not sep or not tty:
            msg = f"session key must name a session (user:tty): {self.session_key!r}"
            raise ValueError(msg)
        if key_user != self.user:
            msg = (
                f"invite user {self.user!r} does not match session-key user "
                f"{key_user!r}"
            )
            raise ValueError(msg)

    @classmethod
    def from_notification(
        cls, notif: TalkNotification, *, arrived: float | None = None
    ) -> Self:
        """Build a pending invite from an invite frame, timing its arrival.

        The frame's session key is validated by ``__post_init__``; a malformed
        frame raises ``ValueError`` at this wire boundary rather than being
        recorded.

        *arrived* preserves the frame's original enqueue time when the invite
        is drained out of the bounded queue into ``talkPending``: without it the
        TTL window would restart at drain time and a stale invite could outlive
        ``PENDING_INVITE_TTL`` (up to ~2x).  Defaults to ``time.monotonic()``
        for the direct-record path where the frame is recorded on arrival.
        """
        return cls(
            user=notif.nfrom,
            session_key=notif.nfrom_key,
            tty=notif.nfrom_tty,
            arrived=time.monotonic() if arrived is None else arrived,
        )

    @property
    def accept_command(self) -> str:
        """A runnable command that accepts this invite by naming the session.

        Prefers the inviter's display tty name (``talk @user:ttyN``) — the
        form ``/who`` shows and ``resolve_talk_target`` matches — so the
        printed hint is exactly what the recipient types.  Falls back to the
        session key when the frame carried no display tty (still a runnable
        ``talk @user:tty`` by the ``HintNamesSession`` invariant).
        """
        if self.tty:
            return f"talk @{self.user}:{self.tty}"
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

        A missing or unrecognized ``type`` yields a wake-poke frame
        (:attr:`is_wake_poke`) — the shape a mail/wall notification takes when
        it rides the talk subject purely to wake the poller.  Such a frame is
        never enqueued or surfaced as a conversation line, so a ``/write`` mail
        body cannot appear as a phantom talk message.  The ``type`` is preserved
        verbatim (defaulting to the empty string) rather than coerced to
        ``message``, which would resurrect that phantom.
        """
        return cls(
            ntype=str(raw.get("type", "")),
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

    @property
    def is_control(self) -> bool:
        """Whether this is a session-scoped control frame.

        Control frames (invite, accept, end, withdraw) carry a target session
        key in production and must name our session to apply.  A typeless
        broadcast message poke (write/wall mail notification) is not control
        and legitimately carries no target key.
        """
        return self.ntype in _CONTROL_TYPES

    @property
    def is_wake_poke(self) -> bool:
        """Whether this frame is a bare wake poke, not a modeled talk frame.

        A ``/write`` or ``/wall`` mail notification rides the talk subject with
        no ``type`` purely to wake the poller; a frame whose ``type`` is missing
        or unrecognized is such a poke.  It wakes the poller so the next tick
        re-checks, but is never enqueued or surfaced as a conversation line —
        otherwise a mail body would appear as a phantom talk message.
        """
        return self.ntype not in _KNOWN_TYPES


@dataclass(frozen=True, slots=True)
class QueuedNotification:
    """A queued notification paired with its monotonic arrival time.

    The arrival time is the time-to-live anchor for an *undrained* invite: on
    the MCP path an invite sits in the bounded queue until ``talk_read`` drains
    it into ``talkPending``, so without an enqueue-time stamp a never-drained
    invite (a crashed inviter that never sends ``ntWithdraw``, an idle-but-alive
    agent) would strand the ``[TALK]`` marker forever.  Stamping at enqueue lets
    the poller reap an aged invite still sitting in the queue — the same
    backstop ``notification.tex`` ``ExpirePendingInvite`` gives the invite it
    models as pending-on-arrival.
    """

    notif: TalkNotification
    arrived: float


@dataclass(frozen=True, slots=True)
class AgentDrain:
    """Result of an MCP agent-mode drain (non-modal front-end).

    The MCP server is not modal like the REPL, so it drains the whole
    queue in one pass: invites are recorded, an accept from the invited
    session connects us, messages are surfaced, and an end resets to idle.
    The connect and reset happen as side effects on the ``TalkState`` during
    the pass; this snapshot carries only what the front-end renders — the
    pending invites and the surfaced messages.
    """

    messages: tuple[TalkNotification, ...]
    pending: Mapping[str, PendingInvite]
