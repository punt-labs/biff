"""The files the spill lives in: read without trust, written without loss."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Self, cast, final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from biff.daemon.durable_file import DurableFile, UnsyncedError
from biff.frame_client import ClientType, Hello
from biff.models import Message
from biff.nats_connection import Scrub

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

__all__ = ["Held", "SpillFile", "SpillKey", "Spilled"]

logger = logging.getLogger(__name__)

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

    def age(self) -> timedelta:
        """How long this message has been waiting."""
        return _now() - self.since


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
        """Return this with *messages* added from now; one already here is not twice."""
        known = {h.message.id for h in self.held}
        added = tuple(Held(message=m) for m in messages if m.id not in known)
        return self.model_copy(update={"held": (*self.held, *added)})

    def merged_with(self, other: Self) -> Self:
        """Return this with what *other* holds that it does not, with their times."""
        known = {h.message.id for h in self.held}
        extra = tuple(h for h in other.held if h.message.id not in known)
        return self.model_copy(update={"held": (*self.held, *extra)})

    def keeping(self, held: Sequence[Held]) -> Self:
        """Return this holding only *held*."""
        return self.model_copy(update={"held": tuple(held)})


@final
class SpillFile:
    """The spill file, a second place to write it, and the shelf beside it.

    This file is the only copy of mail the relay already gave up, so reading it
    never raises, a file or an entry it cannot read is set aside under a name that
    is never reused, and a write that fails is tried at the second place.  Every
    write is a whole snapshot with a higher sequence number, and a read takes the
    snapshot with the highest, so an older file is never mixed with a newer one.
    """

    __slots__ = ("_fallback", "_path", "_seq")

    _path: Path
    _fallback: Path
    _seq: int

    def __new__(cls, path: Path, fallback: Path) -> Self:
        """Keep the spill in *path*, and in *fallback* when *path* cannot be written."""
        self = super().__new__(cls)
        self._path = path
        self._fallback = fallback
        self._seq = 0
        return self

    def read(self) -> dict[SpillKey, Spilled]:
        """Return the newest snapshot; what cannot be read is quarantined."""
        places = (self._fallback, self._path)  # the first place wins a tie
        found = [(self._read_one(p), rank) for rank, p in enumerate(places)]
        (seq, items), _ = max(found, key=lambda f: (f[0][0], f[1]))
        self._seq = max(seq, 0)
        return {item.key: item for item in items}

    def write(self, entries: Mapping[SpillKey, Spilled]) -> None:
        """Make the spill hold *entries*, as the newest snapshot.

        Raises:
            OSError: Neither place could be written.
        """
        kept = [item for item in entries.values() if item.held]
        seq = self._seq + 1
        try:
            self._replace(self._path, kept, seq)
            written, other = self._path, self._fallback
        except OSError:
            logger.critical(
                "could not write the spill to %s; trying %s", self._path, self._fallback
            )
            self._replace(self._fallback, kept, seq)
            written, other = self._fallback, self._path
        self._seq = seq
        self._drop(other)  # the older snapshot; read ignores it if this fails
        if not kept:
            self._drop(written)

    def shelve(self, item: Spilled) -> bool:
        """Keep *item* for good on the shelf beside the spill; say whether it was."""
        shelf = self._shelf()
        held = {i.key: i for i in self._read_one(shelf)[1]}
        earlier = held.get(item.key)
        held[item.key] = item if earlier is None else earlier.merged_with(item)
        try:
            self._replace(shelf, list(held.values()), 0)
        except OSError:
            logger.exception("could not shelve the mail of session %s", item.session_id)
            return False
        return True

    def shelved(self) -> int:
        """Return how many messages are on the shelf."""
        return sum(len(item.held) for item in self._read_one(self._shelf())[1])

    def _shelf(self) -> Path:
        return self._path.with_suffix(".undeliverable")

    @staticmethod
    def _replace(path: Path, items: Sequence[Spilled], seq: int) -> None:
        """Write *items* to *path*; a file whose directory entry is unsynced counts."""
        spilled = [i.model_dump(mode="json") for i in items]
        text = json.dumps({"seq": seq, "spilled": spilled})
        try:
            DurableFile.replace(path.parent, path.name, text)
        except UnsyncedError:
            logger.warning("%s is written; its directory is not synced", path)

    def _drop(self, path: Path) -> None:
        """Delete *path*; one that stays is logged, and is older than the snapshot."""
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not delete the older file %s", path, exc_info=True)

    def _read_one(self, path: Path) -> tuple[int, list[Spilled]]:
        """Return the sequence and the readable entries of *path*; never raises.

        A file that is absent or cannot be read has sequence -1, below any snapshot.
        """
        try:
            seq, items = self._items(path)
        except FileNotFoundError:
            return (-1, [])
        except (OSError, ValueError, KeyError, TypeError):
            logger.exception("the spill file %s cannot be read", path)
            self._set_aside(path, "unreadable")
            return (-1, [])
        good: list[Spilled] = []
        bad: list[object] = []
        for item in items:
            try:
                good.append(Spilled.model_validate(item))
            except ValidationError:
                bad.append(item)
        if bad:
            self._quarantine(path, seq, good, bad)
        return (seq, good)

    @staticmethod
    def _items(path: Path) -> tuple[int, list[object]]:
        """Return the sequence and the raw entries of the file; any trouble raises."""
        data = json.loads(path.read_text())
        items = data["spilled"]
        if not isinstance(items, list):
            msg = "spilled is not a list"
            raise TypeError(msg)
        seq = data.get("seq", 0)
        return (seq if isinstance(seq, int) else 0, list(cast("list[object]", items)))

    def _quarantine(
        self, path: Path, seq: int, good: list[Spilled], bad: list[object]
    ) -> None:
        """Set the unreadable entries aside, and leave *path* with the good ones."""
        messages = sum(self._messages_in(item) for item in bad)
        aside = self._unique(path, "corrupt")
        logger.error(
            "%d spilled sessions (%d messages) in %s cannot be read; set aside in %s",
            len(bad),
            messages,
            path,
            aside,
        )
        try:
            DurableFile.replace(aside.parent, aside.name, json.dumps({"spilled": bad}))
            self._replace(path, good, seq)
        except OSError:
            logger.critical(
                "could not set aside bad entries in %s", path, exc_info=True
            )

    @staticmethod
    def _messages_in(item: object) -> int:
        """Return how many messages a raw entry claims to hold, if it says."""
        if not isinstance(item, dict):
            return 0
        held = cast("dict[str, object]", item).get("held", [])
        return len(cast("list[object]", held)) if isinstance(held, list) else 0

    def _set_aside(self, path: Path, kind: str) -> None:
        """Move the whole file out of the way, under a name no file has had."""
        aside = self._unique(path, kind)
        try:
            path.replace(aside)
        except OSError:
            logger.critical("could not move %s aside to %s", path, aside, exc_info=True)

    @staticmethod
    def _unique(path: Path, kind: str) -> Path:
        """Return a path beside *path* that no file has, so none is overwritten."""
        stamp = time.time_ns()
        while True:
            aside = path.with_name(f"{path.stem}.{kind}-{stamp}{path.suffix}")
            if not aside.exists():
                return aside
            stamp += 1
