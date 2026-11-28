"""The harness sessions biffd holds, kept on disk so a restart does not forget them."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self, final

from pydantic import ValidationError

from biff.frame_client import Hello

if TYPE_CHECKING:
    from biff.daemon.session_key import SessionKey

__all__ = ["HarnessRecords"]

logger = logging.getLogger(__name__)

_NO_MAC: Final = "0" * 64
"""A restored session has no connection, so no proof; the field only has a shape."""
_SUFFIX: Final = ".json"


@final
class HarnessRecords:
    """One small file per harness session: who it is and where it runs.

    Enough to build the session again at daemon start, dormant, so that
    retention, an end, and the mail waiting in its relay inbox still reach it.
    A shell session is not kept here: its record is the CLI session store's.
    """

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, root: Path) -> Self:
        """Keep the records under *root*, created when the first is saved."""
        self = super().__new__(cls)
        self._root = root
        return self

    def save(self, hello: Hello) -> None:
        """Keep the session *hello* opened; saving it again replaces the record."""
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        text = json.dumps(
            hello.model_dump(exclude={"mac", "type"}), sort_keys=True, indent=2
        )
        fd, tmp = tempfile.mkstemp(dir=self._root, prefix=".record-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            Path(tmp).chmod(0o600)
            Path(tmp).replace(self._path(hello.session_id))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def remove(self, key: SessionKey) -> None:
        """Forget the session *key* names; a session that was never kept is fine."""
        self._path(key.session_id).unlink(missing_ok=True)

    def load(self) -> tuple[Hello, ...]:
        """Return the hello of every kept session; an unreadable record is skipped."""
        if not self._root.is_dir():
            return ()
        hellos: list[Hello] = []
        for path in sorted(self._root.glob(f"*{_SUFFIX}")):
            try:
                raw: dict[str, object] = json.loads(path.read_text())
                hellos.append(Hello.model_validate({**raw, "mac": _NO_MAC}))
            except (OSError, ValueError, ValidationError):
                logger.warning(
                    "skipping unreadable session record %s", path, exc_info=True
                )
        return tuple(hellos)

    def _path(self, session_id: str) -> Path:
        # A session id has passed the id charset (``Hello``), so it cannot climb out.
        return self._root / f"{session_id}{_SUFFIX}"
