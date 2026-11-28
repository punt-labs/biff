"""The files the spill lives in: read without trust, written without loss."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Self, cast, final

from pydantic import ValidationError

from biff.daemon.durable_file import DurableFile, UnsyncedError
from biff.daemon.spill_entry import Spilled, SpillKey

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

__all__ = ["SpillFile"]

logger = logging.getLogger(__name__)


@final
class SpillFile:
    """The spill file, a second place to write it, and the shelf beside it.

    This file is the only copy of mail the relay already gave up, so reading it
    never raises, a file or an entry it cannot read is set aside under a name that
    is never reused, and a write that fails is tried at the second place.  Every
    write is a whole snapshot with a higher sequence number, and a read takes the
    snapshot with the highest, so an older file is never mixed with a newer one.
    """

    __slots__ = ("_carried", "_carried_to", "_fallback", "_path", "_seq", "_unread")

    _path: Path
    _fallback: Path
    _seq: int
    _unread: set[Path]
    """Files that exist but could not be read for an I/O reason; never moved or
    replaced, since the mail in them is still there."""
    _carried: list[object]
    """Entries that could not be set aside; every snapshot carries them until then."""
    _carried_to: Path | None

    def __new__(cls, path: Path, fallback: Path) -> Self:
        """Keep the spill in *path*, and in *fallback* when *path* cannot be written."""
        self = super().__new__(cls)
        self._path = path
        self._fallback = fallback
        self._seq = 0
        self._unread = set()
        self._carried = []
        self._carried_to = None
        return self

    def read(self) -> dict[SpillKey, Spilled]:
        """Return the newest snapshot; what cannot be parsed is set aside.

        A file that cannot be read for an I/O reason stays where it is, and the
        log says what that costs.
        """
        before = len(self.asides("unreadable"))
        self._unread = set()
        places = (self._fallback, self._path)  # the later place, the primary, wins ties
        found = [(self._read_one(p), rank) for rank, p in enumerate(places)]
        (seq, items), _ = max(found, key=lambda f: (f[0][0], f[1]))
        self._seq = max(seq, 0)
        if self._unread:
            # A file that could not be read has a sequence we do not know, so the
            # next snapshot must outrank any it can have: the clock does, now and
            # after a restart, and the counter carries on from it.
            self._seq = max(self._seq, time.time_ns())
            self._say_unread(seq, items)
        if len(self.asides("unreadable")) > before and items:
            logger.critical(
                "a spill file could not be parsed and was set aside; the older "
                "snapshot is used, and it may replay mail already filed back"
            )
        return {item.key: item for item in items}

    def _say_unread(self, seq: int, items: Sequence[Spilled]) -> None:
        """Say what an I/O failure reading a snapshot means for the mail."""
        names = ", ".join(str(p) for p in sorted(self._unread))
        if seq < 0:
            logger.critical(
                "the only snapshot of the spill, %s, cannot be read (I/O error); "
                "the mail in it is not in the live spill, and the file is kept",
                names,
            )
            return
        logger.critical(
            "%s cannot be read (I/O error), so mail that only it holds is not in "
            "the live spill; the older snapshot (%d messages) is used and may "
            "replay mail already filed back; the file is kept",
            names,
            sum(len(item.held) for item in items),
        )

    def asides(self, *kinds: str) -> list[Path]:
        """Return the files set aside as *kinds* (every kind if none), both places."""
        found: list[Path] = []
        for path in {self._path, self._fallback}:
            for kind in kinds or ("corrupt", "unreadable"):
                pattern = f"{path.stem}.{kind}-*{path.suffix}"
                found.extend(path.parent.glob(pattern))
        return sorted(found)

    def take_shelf(self) -> list[Spilled]:
        """Return what the shelf holds, for another try; the shelf is left as is."""
        return self._read_one(self._shelf())[1]

    def clear_shelf(self) -> None:
        """Remove the shelf, whose mail is in the spill again."""
        self._drop(self._shelf())

    def write(self, entries: Mapping[SpillKey, Spilled]) -> None:
        """Make the spill hold *entries*, as the newest snapshot.

        Raises:
            OSError: Neither place could be written.
        """
        kept = [item for item in entries.values() if item.held]
        seq = self._seq + 1
        self._settle_carried()
        written = self._write_first(kept, seq)
        other = self._fallback if written == self._path else self._path
        self._seq = seq
        # The older snapshot; read ignores it if this fails.  An empty snapshot is
        # the newest, so it stays until the older file is really gone.
        gone = other not in self._unread and self._drop(other)
        if not kept and gone:
            self._drop(written)

    def _write_first(self, kept: Sequence[Spilled], seq: int) -> Path:
        """Write the snapshot to the first place that takes it; return that place.

        A place whose file could not be read is not replaced: the mail in it is
        still there.
        """
        places = [p for p in (self._path, self._fallback) if p not in self._unread]
        error = OSError("every spill file is unreadable and none is replaced")
        for place in places:
            try:
                self._replace(place, kept, seq, self._carried)
            except OSError as exc:
                error = exc
                logger.critical("could not write the spill to %s", place)
            else:
                return place
        raise error

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
        """Return how many messages are on the shelf; a read that moves nothing."""
        held = self._read_one(self._shelf(), repair=False)[1]
        return sum(len(item.held) for item in held)

    def _shelf(self) -> Path:
        return self._path.with_suffix(".undeliverable")

    @staticmethod
    def _replace(
        path: Path, items: Sequence[Spilled], seq: int, opaque: Sequence[object] = ()
    ) -> None:
        """Write *items*, then *opaque* raw entries, to *path*.

        A file whose directory entry is unsynced counts as written.
        """
        spilled = [*(i.model_dump(mode="json") for i in items), *opaque]
        text = json.dumps({"seq": seq, "spilled": spilled})
        try:
            DurableFile.replace(path.parent, path.name, text)
        except UnsyncedError:
            logger.warning("%s is written; its directory is not synced", path)

    def _drop(self, path: Path) -> bool:
        """Delete *path*; say whether it is gone.  One that stays is logged."""
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not delete the older file %s", path, exc_info=True)
            return False
        return True

    def _read_one(
        self, path: Path, *, repair: bool = True
    ) -> tuple[int, list[Spilled]]:
        """Return the sequence and the readable entries of *path*; never raises.

        A file that is absent or cannot be read has sequence -1, below any snapshot.
        A file that cannot be parsed is set aside, unless *repair* is false (a
        status call moves nothing); one that cannot be read for an I/O reason is
        recorded in :attr:`_unread` and left in place.
        """
        try:
            seq, items = self._items(path)
        except FileNotFoundError:
            return (-1, [])
        except OSError:
            logger.exception("the spill file %s cannot be read (I/O error)", path)
            self._unread.add(path)
            return (-1, [])
        except (ValueError, KeyError, TypeError):
            logger.exception("the spill file %s cannot be parsed", path)
            if repair:
                self._set_aside(path, "unreadable")
            return (-1, [])
        good: list[Spilled] = []
        bad: list[object] = []
        for item in items:
            try:
                good.append(Spilled.model_validate(item))
            except ValidationError:
                bad.append(item)
        if bad and repair:
            self._quarantine(path, seq, good, bad)
        return (seq, good)

    @staticmethod
    def _items(path: Path) -> tuple[int, list[object]]:
        """Return the sequence and the raw entries of the file; any trouble raises."""
        try:
            text = path.read_text()
        except FileNotFoundError:
            raise
        except OSError:
            text = path.read_text()  # once more: EIO and EMFILE are often momentary
        data = json.loads(text)
        items = data["spilled"]
        if not isinstance(items, list):
            msg = "spilled is not a list"
            raise TypeError(msg)
        seq = data.get("seq", 0)
        return (seq if isinstance(seq, int) else 0, list(cast("list[object]", items)))

    def _quarantine(
        self, path: Path, seq: int, good: list[Spilled], bad: list[object]
    ) -> None:
        """Set the unreadable entries aside, and leave *path* with the good ones.

        If they cannot be set aside they are kept in memory and written with every
        snapshot until they are, so that no write drops them.
        """
        messages = sum(self._messages_in(item) for item in bad)
        aside = self._unique(path, "corrupt")
        logger.error(
            "%d spilled sessions (%d messages) in %s cannot be read; set aside in "
            "%s: %s",
            len(bad),
            messages,
            path,
            aside,
            "; ".join(self._describe(item) for item in bad),
        )
        try:
            DurableFile.replace(aside.parent, aside.name, json.dumps({"spilled": bad}))
        except OSError:
            self._carried.extend(bad)
            self._carried_to = aside
            logger.critical(
                "could not set aside bad entries from %s; they are kept and written "
                "with every snapshot until they can be",
                path,
                exc_info=True,
            )
            return
        try:
            self._replace(path, good, seq)
        except OSError:
            logger.critical("could not rewrite %s without the bad entries", path)

    def _settle_carried(self) -> None:
        """Try again to set aside the entries a write must otherwise carry."""
        aside = self._carried_to
        if not self._carried or aside is None:
            return
        try:
            text = json.dumps({"spilled": self._carried})
            DurableFile.replace(aside.parent, aside.name, text)
        except OSError:
            return
        self._carried = []
        self._carried_to = None

    @staticmethod
    def _describe(item: object) -> str:
        """Return a session id and message ids of a raw entry, as far as it has them."""
        if not isinstance(item, dict):
            return repr(item)[:200]
        entry = cast("dict[str, object]", item)
        ids: list[str] = []
        held = entry.get("held")
        for one in cast("list[object]", held) if isinstance(held, list) else ():
            if not isinstance(one, dict):
                continue
            message = cast("dict[str, object]", one).get("message")
            if isinstance(message, dict):
                ids.append(str(cast("dict[str, object]", message).get("id")))
        return f"session {entry.get('session_id')!r} messages {ids}"

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
