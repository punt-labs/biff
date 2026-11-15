"""Filesystem store for CLI session records and their lifecycle transitions."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self, cast, get_args

from biff.session_routing import ID_RE, MAX_ID_LENGTH, SessionIdError

SessionState = Literal["live", "dormant", "ended"]

CLI_ID_PREFIX = "cli-"
"""Reserved prefix: harness session ids never carry it, so a CLI id cannot
collide with a Claude Code id in the shared sessions directory."""

SCHEMA_VERSION = 1
_ID_HEX_CHARS = 16
_RECORD_FILE = "session.json"


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
            session_id=f"{CLI_ID_PREFIX}{secrets.token_hex(_ID_HEX_CHARS // 2)}",
            tty=tty,
            user=user,
            repo=repo,
            repo_root=repo_root,
            hostname=hostname,
            created_at=now,
            state="live",
            last_attached_at=now,
            ended_at=None,
        )

    @classmethod
    def from_json(cls, text: str) -> Self:
        """Parse a stored record, raising ValueError on any malformed field."""
        raw = json.loads(text)
        state = raw["state"]
        if state not in get_args(SessionState):
            msg = f"unknown session state: {state!r}"
            raise ValueError(msg)
        ended = raw["ended_at"]
        return cls(
            version=int(raw["version"]),
            session_id=str(raw["session_id"]),
            tty=str(raw["tty"]),
            user=str(raw["user"]),
            repo=str(raw["repo"]),
            repo_root=str(raw["repo_root"]),
            hostname=str(raw["hostname"]),
            created_at=_parse_time(raw["created_at"]),
            state=cast("SessionState", state),
            last_attached_at=_parse_time(raw["last_attached_at"]),
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
        return replace(self, state="live", last_attached_at=now)

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
        return replace(self, state="ended", ended_at=now)

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
        return cls(Path.home() / ".punt-labs" / "biff" / "sessions")

    def create(self, record: CliSessionRecord) -> None:
        """Persist a new record; refuse an id that already exists."""
        path = self._record_path(record.session_id)
        if path.exists():
            msg = f"session id already exists: {record.session_id}"
            raise FileExistsError(msg)
        self._write(path, record)

    def read(self, session_id: str) -> CliSessionRecord:
        """Return the record for *session_id* or raise UnknownCliSessionError."""
        path = self._record_path(session_id)
        try:
            text = path.read_text()
        except FileNotFoundError:
            msg = f"unknown session: {session_id}"
            raise FileNotFoundError(msg) from None
        return CliSessionRecord.from_json(text)

    def list(self) -> tuple[CliSessionRecord, ...]:
        """Return every stored record, ordered by creation time."""
        if not self.root.is_dir():
            return ()
        records = [
            self.read(d.name)
            for d in self.root.iterdir()
            if d.name.startswith(CLI_ID_PREFIX) and (d / _RECORD_FILE).is_file()
        ]
        return tuple(sorted(records, key=lambda r: r.created_at))

    def attach(self, session_id: str, *, now: datetime) -> CliSessionRecord:
        """Renew a live lease or revive a dormant session; ended raises."""
        return self._apply(session_id, lambda r: r.attached(now))

    def lapse(self, session_id: str) -> CliSessionRecord:
        """Move a live session to dormant; nothing else is released."""
        return self._apply(session_id, lambda r: r.lapsed())

    def end(self, session_id: str, *, now: datetime) -> CliSessionRecord:
        """Mark the session ended, stamping ``ended_at`` once."""
        return self._apply(session_id, lambda r: r.ended(now))

    def expire(self, session_id: str) -> None:
        """Remove a dormant or ended record and its directory."""
        record = self.read(session_id)
        if record.state == "live":
            msg = f"cannot expire live session {session_id}"
            raise ValueError(msg)
        path = self._record_path(session_id)
        path.unlink()
        path.parent.rmdir()

    def _apply(
        self,
        session_id: str,
        step: _Step,
    ) -> CliSessionRecord:
        updated = step(self.read(session_id))
        self._write(self._record_path(session_id), updated)
        return updated

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


def _parse_time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    _require_aware(parsed)
    return parsed.astimezone(UTC)


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None:
        msg = f"datetime must carry a timezone, got {moment!r}"
        raise ValueError(msg)
