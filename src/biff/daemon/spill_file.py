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

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

__all__ = ["SpillFile", "SpillKey", "Spilled"]

logger = logging.getLogger(__name__)

_NO_MAC: Final = "0" * 64
"""A spilled session has no connection, so no proof; the field only has a shape."""

type SpillKey = tuple[str, bool]
"""A session's id, and whether it is an agent's: the two are different inboxes."""


def _now() -> datetime:
    return datetime.now(UTC)


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
    """The session's relay key; empty in a file written before it was kept."""
    relay: str = ""
    """Which relay the mail came from (see :meth:`relay_id`); empty: not known."""
    since: datetime = Field(default_factory=_now)
    messages: tuple[Message, ...]

    @staticmethod
    def relay_id(relay_url: str | None, repo_name: str) -> str:
        """Return what tells one relay from another for a session, without a secret."""
        return f"{relay_url or 'local'}|{repo_name}"

    @property
    def key(self) -> SpillKey:
        """The session this mail was held for."""
        return (self.session_id, self.agent)

    def age(self) -> timedelta:
        """How long this mail has been waiting to be filed."""
        return _now() - self.since

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
        """Return this with *messages* added; one already here is not twice."""
        merged = {m.id: m for m in (*self.messages, *messages)}
        return self.model_copy(update={"messages": tuple(merged.values())})

    def merged_with(self, other: Self) -> Self:
        """Return this and *other*'s mail together, from the earlier of the two."""
        return self.with_mail(other.messages).model_copy(
            update={"since": min(self.since, other.since)}
        )


@final
class SpillFile:
    """The spill file, a second place to write it, and the shelf beside it.

    This file is the only copy of mail the relay already gave up, so reading it
    never raises, a file or an entry it cannot read is set aside under a name that
    is never reused, and a write that fails is tried at the second place.
    """

    __slots__ = ("_fallback", "_path")

    _path: Path
    _fallback: Path

    def __new__(cls, path: Path, fallback: Path) -> Self:
        """Keep the spill in *path*, and in *fallback* when *path* cannot be written."""
        self = super().__new__(cls)
        self._path = path
        self._fallback = fallback
        return self

    def read(self) -> dict[SpillKey, Spilled]:
        """Return what both files hold; what cannot be read is quarantined."""
        entries: dict[SpillKey, Spilled] = {}
        for path in (self._path, self._fallback):
            for item in self._read_one(path):
                held = entries.get(item.key)
                entries[item.key] = item if held is None else held.merged_with(item)
        return entries

    def write(self, entries: Mapping[SpillKey, Spilled]) -> None:
        """Make the spill hold *entries*; none, no file.

        Raises:
            OSError: Neither place could be written.
        """
        kept = [item for item in entries.values() if item.messages]
        if not kept:
            self._path.unlink(missing_ok=True)
            self._fallback.unlink(missing_ok=True)
            return
        try:
            self._replace(self._path, kept)
        except OSError:
            logger.critical(
                "could not write the spill to %s; trying %s", self._path, self._fallback
            )
            self._replace(self._fallback, kept)
            return
        self._fallback.unlink(missing_ok=True)  # the spill is whole in the first place

    def shelve(self, item: Spilled) -> bool:
        """Keep *item* for good on the shelf beside the spill; say whether it was."""
        shelf = self._path.with_suffix(".undeliverable")
        held = {i.key: i for i in self._read_one(shelf)}
        earlier = held.get(item.key)
        held[item.key] = item if earlier is None else earlier.merged_with(item)
        try:
            self._replace(shelf, list(held.values()))
        except OSError:
            logger.exception("could not shelve the mail of session %s", item.session_id)
            return False
        return True

    @staticmethod
    def _replace(path: Path, items: Sequence[Spilled]) -> None:
        """Write *items* to *path*; a file whose directory entry is unsynced counts."""
        text = json.dumps({"spilled": [i.model_dump(mode="json") for i in items]})
        try:
            DurableFile.replace(path.parent, path.name, text)
        except UnsyncedError:
            logger.warning("%s is written; its directory is not synced", path)

    def _read_one(self, path: Path) -> list[Spilled]:
        """Return the entries of *path* that can be read; never raises."""
        try:
            items = self._items(path)
        except FileNotFoundError:
            return []
        except (OSError, ValueError, KeyError, TypeError):
            logger.exception("the spill file %s cannot be read", path)
            self._set_aside(path, "unreadable")
            return []
        good: list[Spilled] = []
        bad: list[object] = []
        for item in items:
            try:
                good.append(Spilled.model_validate(item))
            except ValidationError:
                bad.append(item)
        if bad:
            self._quarantine(path, good, bad)
        return good

    @staticmethod
    def _items(path: Path) -> list[object]:
        """Return the raw entries of the file; any trouble raises."""
        items = json.loads(path.read_text())["spilled"]
        if not isinstance(items, list):
            msg = "spilled is not a list"
            raise TypeError(msg)
        return list(cast("list[object]", items))

    def _quarantine(self, path: Path, good: list[Spilled], bad: list[object]) -> None:
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
            self._replace(path, good)
        except OSError:
            logger.critical(
                "could not set aside bad entries in %s", path, exc_info=True
            )

    @staticmethod
    def _messages_in(item: object) -> int:
        """Return how many messages a raw entry claims to hold, if it says."""
        if not isinstance(item, dict):
            return 0
        messages = cast("dict[str, object]", item).get("messages", [])
        return len(cast("list[object]", messages)) if isinstance(messages, list) else 0

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
