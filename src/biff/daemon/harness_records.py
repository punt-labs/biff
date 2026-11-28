"""The harness sessions biffd holds, kept on disk so a restart does not forget them."""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self, final

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from biff.daemon.refusal import RefusalError
from biff.frame_client import Hello
from biff.frame_reply import ErrorCode

if TYPE_CHECKING:
    from biff.daemon.session_key import SessionKey

__all__ = ["HarnessRecords", "KeptSession"]

logger = logging.getLogger(__name__)

_NO_MAC: Final = "0" * 64
"""A restored session has no connection, so no proof; the field only has a shape."""
_SUFFIX: Final = ".json"


class _Stored(BaseModel):
    """What one record file holds; anything else in the file is refused."""

    model_config = ConfigDict(extra="forbid")

    hello: dict[str, JsonValue]
    dormant_since: datetime | None = None
    failed_starts: int = 0


@dataclass(frozen=True, slots=True)
class KeptSession:
    """A session kept across a restart, and what the daemon knew of it."""

    hello: Hello
    dormant_since: datetime | None
    """When its last connection left; ``None``: one was on it when written."""
    failed_starts: int
    """How many starts in a row could not build it again."""


@final
class HarnessRecords:
    """One small file per harness session: who it is, where it runs, since when idle.

    Enough to build the session again at daemon start, dormant, so that
    retention, an end, and the mail waiting in its relay inbox still reach it.
    A shell session is not kept here: its record is the CLI session store's.
    """

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, root: Path) -> Self:
        """Keep the records under *root*, created when the first is kept."""
        self = super().__new__(cls)
        self._root = root
        return self

    def keep(self, hello: Hello) -> None:
        """Keep the new session *hello* opened, with a connection on it.

        Raises:
            RefusalError: The record cannot be written; the session is then not
                opened, since a restart would forget it.
        """
        stored = _Stored(hello=hello.model_dump(exclude={"mac", "type"}))
        try:
            self._write(self._path(hello.session_id), stored)
        except OSError as exc:
            msg = f"cannot keep the session record in {self._root}: {exc}"
            raise RefusalError.of(ErrorCode.INTERNAL, msg) from exc

    def mark(self, key: SessionKey, dormant_since: datetime | None) -> None:
        """Record when the session's last connection left (``None``: one is on it)."""
        self._update(key, dormant_since=dormant_since)

    def failed(self, key: SessionKey) -> int:
        """Count one more start that could not build the session; return the count."""
        count = self._failed_starts(key) + 1
        self._update(key, failed_starts=count)
        return count

    def remove(self, key: SessionKey) -> None:
        """Forget the session *key* names; a session that was never kept is fine.

        A record that cannot be deleted is logged and left: the end that called
        this has already happened, and a restart would restore the session until
        retention ends it again.
        """
        path = self._path(key.session_id)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.exception(
                "could not delete the record of session %s at %s", key.session_id, path
            )

    def load(self) -> tuple[KeptSession, ...]:
        """Return every kept session; an unreadable record is skipped and logged."""
        if not self._root.is_dir():
            return ()
        kept: list[KeptSession] = []
        for path in sorted(self._root.glob(f"*{_SUFFIX}")):
            try:
                kept.append(self._parse(path))
            except (OSError, ValueError, ValidationError):
                logger.warning(
                    "skipping unreadable session record %s", path, exc_info=True
                )
        return tuple(kept)

    def _failed_starts(self, key: SessionKey) -> int:
        try:
            return self._read(self._path(key.session_id)).failed_starts
        except (OSError, ValueError, ValidationError):
            return 0

    def _update(self, key: SessionKey, **changes: datetime | int | None) -> None:
        """Rewrite one record with *changes*; a failure is logged, never raised."""
        path = self._path(key.session_id)
        try:
            self._write(path, self._read(path).model_copy(update=changes))
        except (OSError, ValueError, ValidationError):
            logger.warning("could not update the record at %s", path, exc_info=True)

    def _parse(self, path: Path) -> KeptSession:
        stored = self._read(path)
        hello = Hello.model_validate({**stored.hello, "mac": _NO_MAC})
        since = stored.dormant_since
        return KeptSession(
            hello=hello,
            dormant_since=None if since is None else since.astimezone(UTC),
            failed_starts=stored.failed_starts,
        )

    @staticmethod
    def _read(path: Path) -> _Stored:
        return _Stored.model_validate_json(path.read_text())

    def _write(self, path: Path, stored: _Stored) -> None:
        """Replace the file at *path* with *stored*, durably: data, then name."""
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._root, prefix=".record-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(stored.model_dump_json(indent=2))
                fh.flush()
                os.fsync(fh.fileno())
            Path(tmp).chmod(0o600)
            Path(tmp).replace(path)
            self._sync_directory()
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _sync_directory(self) -> None:
        fd = os.open(self._root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _path(self, session_id: str) -> Path:
        # A session id has passed the id charset (``Hello``), so it cannot climb out.
        return self._root / f"{session_id}{_SUFFIX}"
