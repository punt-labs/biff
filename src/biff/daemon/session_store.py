"""Filesystem store for CLI session records and their lifecycle transitions."""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self, cast, get_args

from biff._stdlib import biff_data_dir
from biff.daemon.session_lock import SessionLock
from biff.session_routing import ID_RE, MAX_ID_LENGTH, SessionIdError
from biff.tty import CLI_ID_HEX_CHARS, CLI_ID_PREFIX, CLI_ID_RE

logger = logging.getLogger(__name__)

SessionState = Literal["live", "dormant", "ended"]

SCHEMA_VERSION = 1
_RECORD_FILE = "session.json"
_LOCK_DIR = "locks"


class CliSessionEndedError(ValueError):
    """The session has ended; an ended session is never revived."""


@dataclass(frozen=True, slots=True)
class CliSessionRecord:
    """One CLI session.

    ``(session_id, tty, user, repo, created_at)`` is fixed at creation; only
    ``state``, ``last_attached_at`` and ``ended_at`` change afterwards.
    """

    version: int
    session_id: str
    tty: str
    user: str
    repo: str
    repo_root: str
    hostname: str
    created_at: datetime
    state: SessionState
    last_attached_at: datetime
    ended_at: datetime | None  # absent until the session ends

    @classmethod
    def new(
        cls,
        *,
        tty: str,
        user: str,
        repo: str,
        repo_root: str,
        hostname: str,
        now: datetime,
    ) -> Self:
        """Return a fresh live record with a newly minted session_id."""
        _require_aware(now)
        return cls(
            version=SCHEMA_VERSION,
            session_id=f"{CLI_ID_PREFIX}{secrets.token_hex(CLI_ID_HEX_CHARS // 2)}",
            tty=tty,
            user=user,
            repo=repo,
            repo_root=repo_root,
            hostname=hostname,
            created_at=now.astimezone(UTC),
            state="live",
            last_attached_at=now.astimezone(UTC),
            ended_at=None,
        )

    @classmethod
    def from_json(cls, text: str) -> Self:
        """Parse a stored record; any malformed or missing field raises ValueError."""
        try:
            return cls._from_raw(json.loads(text))
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            msg = f"malformed session record: {exc!r}"
            raise ValueError(msg) from exc

    @classmethod
    def _from_raw(cls, raw: dict[str, object]) -> Self:
        state = raw["state"]
        if state not in get_args(SessionState):
            msg = f"unknown session state: {state!r}"
            raise ValueError(msg)
        if raw["version"] != SCHEMA_VERSION or isinstance(raw["version"], bool):
            msg = f"unsupported schema version: {raw['version']!r}"
            raise ValueError(msg)
        ended = raw["ended_at"]
        return cls(
            version=SCHEMA_VERSION,
            session_id=_require_str(raw, "session_id"),
            tty=_require_str(raw, "tty"),
            user=_require_str(raw, "user"),
            repo=_require_str(raw, "repo"),
            repo_root=_require_str(raw, "repo_root"),
            hostname=_require_str(raw, "hostname"),
            created_at=_parse_time(_require_str(raw, "created_at")),
            state=cast("SessionState", state),
            last_attached_at=_parse_time(_require_str(raw, "last_attached_at")),
            ended_at=None if ended is None else _parse_time(ended),
        )

    def to_json(self) -> str:
        """Serialize to the on-disk JSON form."""
        return json.dumps(
            {
                "version": self.version,
                "session_id": self.session_id,
                "tty": self.tty,
                "user": self.user,
                "repo": self.repo,
                "repo_root": self.repo_root,
                "hostname": self.hostname,
                "created_at": self.created_at.isoformat(),
                "state": self.state,
                "last_attached_at": self.last_attached_at.isoformat(),
                "ended_at": None
                if self.ended_at is None
                else self.ended_at.isoformat(),
            },
            indent=2,
        )

    def attached(self, now: datetime) -> Self:
        """Return the record after an attach: live, lease renewed."""
        if self.state == "ended":
            raise self.ended_error()
        _require_aware(now)
        return replace(self, state="live", last_attached_at=now.astimezone(UTC))

    def lapsed(self) -> Self:
        """Return the record after its lease lapsed: dormant."""
        if self.state == "ended":
            raise self.ended_error()
        return replace(self, state="dormant")

    def ended(self, now: datetime) -> Self:
        """Return the record after ``end``; an already-ended record is unchanged."""
        if self.state == "ended":
            return self
        _require_aware(now)
        return replace(self, state="ended", ended_at=now.astimezone(UTC))

    def renamed(self, tty: str) -> Self:
        """Return the record under a new display name; an ended record refuses."""
        if self.state == "ended":
            raise self.ended_error()
        return replace(self, tty=tty)

    def ended_error(self) -> CliSessionEndedError:
        """Return the error naming when this session ended and how to recover."""
        msg = (
            f"session {self.tty} ended at {self.ended_at}; "
            f"run biff session new --tty {self.tty}"
        )
        return CliSessionEndedError(msg)


type _Step = Callable[[CliSessionRecord], CliSessionRecord]


@dataclass(frozen=True, slots=True)
class CliSessionStore:
    """One JSON record per session under ``root/<session_id>/session.json``."""

    root: Path

    @classmethod
    def default(cls) -> Self:
        """Return the store at ``~/.punt-labs/biff/sessions``."""
        return cls(biff_data_dir() / "sessions")

    def create(self, record: CliSessionRecord) -> None:
        """Persist a new record; refuse an id that exists or is not a CLI id."""
        if not CLI_ID_RE.fullmatch(record.session_id):
            msg = (
                f"session_id must be {CLI_ID_PREFIX!r} plus {CLI_ID_HEX_CHARS} "
                f"hex characters, got {record.session_id!r}"
            )
            raise ValueError(msg)
        path = self._record_path(record.session_id)
        with self._locked(record.session_id):
            if path.exists():
                msg = f"session id already exists: {record.session_id}"
                raise FileExistsError(msg)
            self._write(path, record)

    def read(self, session_id: str) -> CliSessionRecord:
        """Return the record for *session_id*; FileNotFoundError when absent."""
        path = self._record_path(session_id)
        try:
            text = path.read_text()
        except FileNotFoundError:
            msg = f"unknown session: {session_id}"
            raise FileNotFoundError(msg) from None
        record = CliSessionRecord.from_json(text)
        if record.session_id != session_id:
            msg = (
                f"malformed session record: id mismatch, directory "
                f"{session_id!r} holds {record.session_id!r}"
            )
            raise ValueError(msg)
        return record

    def list(self) -> tuple[CliSessionRecord, ...]:
        """Return every stored record, ordered by creation time."""
        if not self.root.is_dir():
            return ()
        records: list[CliSessionRecord] = []
        for d in self.root.iterdir():
            if not CLI_ID_RE.fullmatch(d.name):
                continue
            try:
                records.append(self.read(d.name))
            except FileNotFoundError:
                continue  # expired between the scan and the read
            except ValueError:
                # One unreadable record (corrupt, or written by a newer biff)
                # must not make every command fail.
                logger.warning("skipping unreadable session %s", d.name, exc_info=True)
        return tuple(sorted(records, key=lambda r: r.created_at))

    def attach(self, session_id: str, *, now: datetime) -> CliSessionRecord:
        """Renew a live lease or revive a dormant session; ended raises."""
        return self._apply(session_id, lambda r: r.attached(now))

    def rename(self, session_id: str, tty: str) -> CliSessionRecord:
        """Change the session's display name, keeping its id and everything else."""
        return self._apply(session_id, lambda r: r.renamed(tty))

    def lapse(self, session_id: str) -> CliSessionRecord:
        """Move a live session to dormant; nothing else is released."""
        return self._apply(session_id, lambda r: r.lapsed())

    def end(self, session_id: str, *, now: datetime) -> CliSessionRecord:
        """Mark the session ended, stamping ``ended_at`` once."""
        return self._apply(session_id, lambda r: r.ended(now))

    def expire(self, session_id: str) -> None:
        """Remove a dormant or ended record and its directory."""
        path = self._record_path(session_id)
        self._require_known(session_id)
        with self._locked(session_id):
            if self.read(session_id).state == "live":
                msg = f"cannot expire live session {session_id}"
                raise ValueError(msg)
            path.unlink()
            path.parent.rmdir()

    def _apply(self, session_id: str, step: _Step) -> CliSessionRecord:
        path = self._record_path(session_id)
        self._require_known(session_id)
        # Held across read, compute and write so a concurrent process cannot
        # write back a stale state (e.g. revive a session another just ended).
        with self._locked(session_id):
            updated = step(self.read(session_id))
            self._write(path, updated)
        return updated

    def _require_known(self, session_id: str) -> None:
        # Cheap pre-check so an unknown id never creates a lock file.
        if not (self.root / session_id).is_dir():
            msg = f"unknown session: {session_id}"
            raise FileNotFoundError(msg)

    @property
    def _lock(self) -> SessionLock:
        return SessionLock(self.root / _LOCK_DIR)

    @asynccontextmanager
    async def lifecycle(self, session_id: str) -> AsyncGenerator[None]:
        """Hold the session's lock across a transition AND its relay side effects."""
        self._record_path(session_id)  # validates the id before any path is built
        async with self._lock.hold(session_id):
            yield

    def _locked(self, session_id: str) -> AbstractContextManager[None]:
        return self._lock.hold_blocking(session_id)

    def _record_path(self, session_id: str) -> Path:
        # Validate before building any path so a hostile id cannot escape root.
        if not ID_RE.fullmatch(session_id):
            msg = (
                f"session_id must match [A-Za-z0-9_-]+ and be at most "
                f"{MAX_ID_LENGTH} characters, got {session_id!r}"
            )
            raise SessionIdError(msg)
        return self.root / session_id / _RECORD_FILE

    def _write(self, path: Path, record: CliSessionRecord) -> None:
        directory = path.parent
        directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".session-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(record.to_json())
            Path(tmp).chmod(0o600)
            Path(tmp).replace(path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def _require_str(raw: dict[str, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str):
        msg = f"field {key!r} must be a string, got {value!r}"
        raise ValueError(msg)
    return value


def _parse_time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    _require_aware(parsed)
    return parsed.astimezone(UTC)


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None:
        msg = f"datetime must carry a timezone, got {moment!r}"
        raise ValueError(msg)
