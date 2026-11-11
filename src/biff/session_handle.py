"""Per-session handle file: ``<root>/<session_id>/handle``, private to the user."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from biff.session_routing import SessionRouting

_DIR_MODE = 0o700
_FILE_MODE = 0o600


@dataclass(frozen=True, slots=True)
class SessionHandleStore:
    """Reads and writes the handle the SessionStart hook leaves for a session.

    The handle is the session key the opener registered.  The file is
    written through a temporary name and renamed into place, in directories
    that only the user can enter, as the caller-identity standard requires.
    ``SessionRouting`` has already checked ``session_id`` against
    ``[A-Za-z0-9_-]+``, so it is safe as a path component.

    Nothing at runtime reads this yet.  It is the engine-session handle
    store of the caller-identity standard (section 4.2.4), written now so a
    layer-2 engine session and the SessionEnd handoff have a handle to find;
    it is intentional forward wiring, not dead state.
    """

    root: Path

    def path(self, routing: SessionRouting) -> Path:
        """Location of *routing*'s handle file."""
        return self.root / routing.session_id / "handle"

    def write(self, routing: SessionRouting, handle: str) -> Path:
        """Store *handle* for *routing*, replacing any earlier value."""
        target = self.path(routing)
        for directory in (self.root, target.parent):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(_DIR_MODE)
        tmp = target.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(handle)
            tmp.replace(target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return target

    def read(self, routing: SessionRouting) -> str | None:
        """Return the stored handle; ``None`` means this session was never opened."""
        try:
            return self.path(routing).read_text()
        except FileNotFoundError:
            return None
