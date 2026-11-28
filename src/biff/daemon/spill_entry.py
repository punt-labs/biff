"""What the spill holds: one session's unfiled mail, message by message."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar, Final, Self

from pydantic import BaseModel, ConfigDict, Field

from biff.frame_client import ClientType, Hello
from biff.models import Message
from biff.nats_connection import Scrub

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["Held", "SpillKey", "Spilled", "Tries"]

_NO_MAC: Final = "0" * 64
"""A spilled session has no connection, so no proof; the field only has a shape."""

type SpillKey = tuple[str, bool]
"""A session's id, and whether it is an agent's: the two are different inboxes."""


def _now() -> datetime:
    return datetime.now(UTC)


class Held(BaseModel):
    """One spilled message, and since when it has been waiting to be filed."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    message: Message
    since: datetime = Field(default_factory=_now)
    attempts: int = 0
    """How many times filing it back failed; a week with no try is not a failure."""
    failing_for: float = 0.0
    """Seconds the daemon spent failing to file it: the gaps between failed tries,
    each counted for at most :attr:`MAX_GAP`, so days of downtime are not failing."""
    last_failed: datetime | None = None  # absent until the first failed try
    """When the latest failed try was."""

    MAX_GAP: ClassVar[timedelta] = timedelta(minutes=10)

    def age(self) -> timedelta:
        """How long this message has been waiting."""
        return _now() - self.since

    def failing(self) -> timedelta:
        """How long the daemon has been failing to file this message."""
        return timedelta(seconds=self.failing_for)

    def failed(self) -> Self:
        """Return this after one more failed try at filing it back."""
        now = _now()
        gap = timedelta(0) if self.last_failed is None else now - self.last_failed
        return self.model_copy(
            update={
                "attempts": self.attempts + 1,
                "failing_for": self.failing_for
                + min(gap, self.MAX_GAP).total_seconds(),
                "last_failed": now,
            }
        )

    def renewed(self) -> Self:
        """Return this as new: no failed tries, waiting from now."""
        return self.model_copy(
            update={
                "attempts": 0,
                "failing_for": 0.0,
                "last_failed": None,
                "since": _now(),
            }
        )


class Spilled(BaseModel):
    """The mail one session still had unfiled, and what finds its relay again.

    Fields a newer biff added are ignored, so a file it wrote stays readable.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    session_id: str
    agent: bool
    cwd: str
    user: str
    session_key: str = ""
    """The session's relay key; empty when it is not known."""
    relay: str = ""
    """Which relay the mail came from (see :meth:`relay_id`); empty: not known."""
    held: tuple[Held, ...]

    @staticmethod
    def relay_id(relay_url: str | None, repo_name: str) -> str:
        """Return what tells one relay from another: a host and port, no secret."""
        host = "local" if relay_url is None else Scrub.relay_url(relay_url)
        return f"{host}|{repo_name}"

    @property
    def key(self) -> SpillKey:
        """The session this mail was held for."""
        return (self.session_id, self.agent)

    @property
    def messages(self) -> tuple[Message, ...]:
        """The spilled messages, oldest spill first."""
        return tuple(h.message for h in self.held)

    def hello(self) -> Hello:
        """Return the hello that resolves to this session's relay."""
        client: ClientType = "mcp" if self.agent else "cli"
        return Hello(
            mac=_NO_MAC,
            client=client,
            session_id=self.session_id,
            cwd=self.cwd,
            user=self.user,
        )

    def with_mail(self, messages: Sequence[Message]) -> Self:
        """Return this with *messages* added from now; one already here is not twice.

        A message that is the refiled copy of one held here replaces it: filing
        the original later would reuse the copy's id, which the relay may drop.
        """
        copies = {m.id for m in messages}
        kept = tuple(h for h in self.held if h.message.refiled().id not in copies)
        known = {h.message.id for h in kept}
        added = tuple(Held(message=m) for m in messages if m.id not in known)
        return self.model_copy(update={"held": (*kept, *added)})

    def without(self, gone: Sequence[Message]) -> Self:
        """Return this without *gone*, which was filed back."""
        ids = {m.id for m in gone}
        return self.keeping([h for h in self.held if h.message.id not in ids])

    def merged_with(self, other: Self) -> Self:
        """Return this with what *other* holds that it does not, with their times."""
        known = {h.message.id for h in self.held}
        extra = tuple(h for h in other.held if h.message.id not in known)
        return self.model_copy(update={"held": (*self.held, *extra)})

    def keeping(self, held: Sequence[Held]) -> Self:
        """Return this holding only *held*."""
        return self.model_copy(update={"held": tuple(held)})


@dataclass(slots=True)
class Tries:
    """What one pass at filing a session's mail did, kept even if the pass is cut."""

    filed: set[object] = field(default_factory=set[object])
    tried: set[object] = field(default_factory=set[object])
    session_failed: bool = False
    """The session could not be reached at all: every message of it failed."""
