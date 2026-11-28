"""The local relay's TTY name reservations and session-id reclaim hints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from biff.relay_config import RelayLocation
from biff.tty import validate_reclaimable_name, validate_routing_id

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

__all__ = ["LocalTtyNames"]


@final
class LocalTtyNames:
    """Lockfiles that reserve TTY names, and files that hint which a session had."""

    __slots__ = ("_dir", "_validate_user")

    _dir: Path
    _validate_user: Callable[[str], str]

    def __new__(cls, data_dir: Path, validate_user: Callable[[str], str]) -> Self:
        """Keep the files in *data_dir*, accepting only users *validate_user* does."""
        self = super().__new__(cls)
        self._dir = data_dir
        self._validate_user = validate_user
        return self

    def reserve(self, user: str, name: str, session_key: str) -> bool:
        """Reserve *name* atomically via an O_CREAT|O_EXCL lockfile."""
        RelayLocation.ensure(self._dir)
        path = self._lock_path(user, name)
        try:
            with path.open("x") as fd:
                fd.write(session_key)
            path.chmod(0o600)
            return True
        except FileExistsError:
            return False

    def release(self, user: str, name: str) -> None:
        """Release *name* by removing its lockfile."""
        self._lock_path(user, name).unlink(missing_ok=True)

    def release_if(self, user: str, name: str, owner: str) -> bool:
        """Remove *name*'s lockfile only if *owner* wrote it; say whether it did."""
        if self.owner(user, name) != owner:
            return False
        self.release(user, name)
        return True

    def refresh(self, user: str, name: str, session_key: str) -> None:
        """Rewrite the lockfile, only if *session_key* is its current owner.

        This keeps a reservation another session legitimately claimed after a
        TTL lapse from being clobbered.
        """
        path = self._lock_path(user, name)
        if path.exists() and path.read_text() == session_key:
            path.write_text(session_key)
            path.chmod(0o600)

    def owner(self, user: str, name: str) -> str | None:
        """Return the session key that holds *name*, or ``None``."""
        path = self._lock_path(user, name)
        return path.read_text() if path.exists() else None

    def reserved(self, user: str) -> list[str]:
        """List the names *user* has reserved, by globbing the lockfiles.

        The ``sidmap-`` hint files use a distinct prefix, so the ``ttyname-``
        glob excludes them.
        """
        self._validate_user(user)
        if not self._dir.exists():
            return []
        prefix, suffix = f"ttyname-{user}-", ".lock"
        found = (
            p.name.removeprefix(prefix).removesuffix(suffix)
            for p in self._dir.glob(f"{prefix}*{suffix}")
        )
        return [name for name in found if name]

    def hint(self, user: str, session_id: str) -> str | None:
        """Return the last name *session_id* claimed, or ``None``."""
        try:
            return self._hint_path(user, session_id).read_text().strip() or None
        except OSError:
            return None

    def set_hint(self, user: str, session_id: str, name: str) -> None:
        """Record the name *session_id* claimed, overwriting.

        The name is validated before it is written: it is read back as a reclaim
        candidate and reserved as a key.
        """
        error = validate_reclaimable_name(name)
        if error is not None:
            raise ValueError(error)
        RelayLocation.ensure(self._dir)
        hint = self._hint_path(user, session_id)
        hint.write_text(name)
        hint.chmod(0o600)

    def _lock_path(self, user: str, name: str) -> Path:
        self._validate_user(user)
        return self._dir / f"ttyname-{user}-{name}.lock"

    def _hint_path(self, user: str, session_id: str) -> Path:
        """Return the file for a ``session_id -> name`` hint.

        The id is validated as ``NatsRelay`` validates its hint key, so both
        backends reject the same inputs; a valid routing id is hex and dashes,
        free of separators and traversal.
        """
        self._validate_user(user)
        error = validate_routing_id(session_id)
        if error is not None:
            raise ValueError(error)
        return self._dir / f"sidmap-{user}-{session_id}"
