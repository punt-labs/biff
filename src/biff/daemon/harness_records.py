"""The harness sessions biffd holds, kept on disk so a restart does not forget them."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self, final

from pydantic import BaseModel, ConfigDict, ValidationError

from biff.daemon.durable_file import DurableFile, UnsyncedError
from biff.daemon.refusal import RefusalError
from biff.frame_client import ClientType, Hello
from biff.frame_reply import ErrorCode
from biff.relay_seed import NO_RELAY_SETTINGS

if TYPE_CHECKING:
    from collections.abc import Callable

    from biff.daemon.session_key import SessionKey
    from biff.relay_seed import RelaySeed

__all__ = ["HarnessRecords", "KeptSession"]

logger = logging.getLogger(__name__)

_NO_MAC: Final = "0" * 64
"""A restored session has no connection, so no proof; the field only has a shape."""
_SUFFIX: Final = ".json"


class _StoredHello(BaseModel):
    """Who a kept session is: the hello's fields, without its proof."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client: ClientType
    session_id: str
    main_agent_id: str
    cwd: str
    user: str


class _Stored(BaseModel):
    """What one record file holds; anything else in the file is refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hello: _StoredHello
    dormant_since: datetime | None = None
    failed_starts: int = 0
    # biffd's seed identity when it opened the session: the variable names, the
    # relay's scheme and host, and one keyed hash of all the settings.  The file
    # holds no secret and no bare digest of one.
    relay_seed: str = NO_RELAY_SETTINGS.describe()


@dataclass(frozen=True, slots=True)
class KeptSession:
    """A session kept across a restart, and what the daemon knew of it."""

    hello: Hello
    dormant_since: datetime | None
    """When its last connection left; ``None``: one was on it when written."""
    failed_starts: int
    """How many starts in a row could not build it again."""
    relay_seed: str
    """biffd's seed identity when it opened the session (``RelaySeed.identity``)."""


@final
class HarnessRecords:
    """One small file per harness session: who it is, where it runs, since when idle.

    Enough to build the session again at daemon start, dormant, so that
    retention, an end, and the mail waiting in its relay inbox still reach it.
    A shell session is not kept here: its record is the CLI session store's.
    """

    __slots__ = ("_root", "_seed")

    _root: Path
    _seed: str

    def __new__(cls, root: Path, seed: RelaySeed = NO_RELAY_SETTINGS) -> Self:
        """Keep the records under *root*, created when the first is kept.

        Each record is tagged with *seed*'s identity, the relay settings biffd
        holds as it opens the session.
        """
        self = super().__new__(cls)
        self._root = root
        self._seed = seed.identity()
        return self

    def keep(self, hello: Hello) -> None:
        """Keep the new session *hello* opened, with a connection on it.

        Raises:
            RefusalError: The record cannot be written; the session is then not
                opened, since a restart would forget it.
        """
        stored = _Stored(
            hello=_StoredHello(
                client=hello.client,
                session_id=hello.session_id,
                main_agent_id=hello.main_agent_id,
                cwd=hello.cwd,
                user=hello.user,
            ),
            relay_seed=self._seed,
        )
        try:
            self._write(hello.session_id, stored)
        except OSError as exc:
            msg = f"cannot keep the session record in {self._root}: {exc}"
            raise RefusalError.of(ErrorCode.INTERNAL, msg) from exc

    def mark(self, key: SessionKey, dormant_since: datetime | None) -> None:
        """Record when the session's last connection left (``None``: one is on it)."""
        self._change(
            key,
            lambda s: s.model_copy(
                update={"dormant_since": dormant_since, "failed_starts": 0}
            ),
        )

    def attached(self, key: SessionKey, run: str) -> None:
        """Record that a connection is on the session, and the run it claims.

        An empty *run* is a client that claims none, and leaves the kept run be.
        """

        def step(stored: _Stored) -> _Stored:
            hello = stored.hello
            if run:
                hello = hello.model_copy(update={"main_agent_id": run})
            return stored.model_copy(
                update={"hello": hello, "dormant_since": None, "failed_starts": 0}
            )

        self._change(key, step)

    def failed(self, key: SessionKey) -> int:
        """Count one more start that could not build the session; return the count."""
        stored = self._change(
            key, lambda s: s.model_copy(update={"failed_starts": s.failed_starts + 1})
        )
        return 1 if stored is None else stored.failed_starts

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

    def _change(
        self, key: SessionKey, step: Callable[[_Stored], _Stored]
    ) -> _Stored | None:
        """Rewrite one record with *step* applied; ``None``: it could not be.

        A failure is logged, never raised: the record is a promise for a restart,
        and the session in memory is fine without it.
        """
        path = self._path(key.session_id)
        try:
            changed = step(self._read(path))
            self._write(key.session_id, changed)
        except FileNotFoundError:
            # The session ended and its record went with it; a connection that
            # leaves afterwards has nothing to mark.
            logger.debug("no record at %s to update", path)
            return None
        except (OSError, ValueError, ValidationError):
            logger.warning("could not update the record at %s", path, exc_info=True)
            return None
        return changed

    @staticmethod
    def _parse(path: Path) -> KeptSession:
        stored = HarnessRecords._read(path)
        hello = Hello(mac=_NO_MAC, **stored.hello.model_dump())
        since = stored.dormant_since
        return KeptSession(
            hello=hello,
            dormant_since=None if since is None else since.astimezone(UTC),
            failed_starts=stored.failed_starts,
            relay_seed=stored.relay_seed,
        )

    @staticmethod
    def _read(path: Path) -> _Stored:
        return _Stored.model_validate_json(path.read_text())

    def _write(self, session_id: str, stored: _Stored) -> None:
        """Replace the session's record with *stored*, durably."""
        name = f"{session_id}{_SUFFIX}"
        try:
            DurableFile.replace(self._root, name, stored.model_dump_json(indent=2))
        except UnsyncedError:
            logger.warning("record %s written; its directory entry is not synced", name)

    def _path(self, session_id: str) -> Path:
        # A session id has passed the id charset (``Hello``), so it cannot climb out.
        return self._root / f"{session_id}{_SUFFIX}"
