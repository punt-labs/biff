"""The per-repo pointer naming the CLI session a bare ``biff`` command attaches to."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from biff._stdlib import biff_data_dir

_KEY_HEX_CHARS = 16


@dataclass(frozen=True, slots=True)
class CurrentPointer:
    """One file per repo under *root*, each holding a single session id.

    Keyed by repo, not global: a single pointer would attach ``biff who`` in
    repo B to a session created for repo A.
    """

    root: Path

    @classmethod
    def default(cls) -> Self:
        """Return the pointer directory at ``~/.punt-labs/biff/current``."""
        return cls(biff_data_dir() / "current")

    @staticmethod
    def repo_key(repo_root: Path) -> str:
        """Return a 16-hex digest of the resolved repo root."""
        digest = hashlib.sha256(str(repo_root.resolve()).encode()).hexdigest()
        return digest[:_KEY_HEX_CHARS]

    def read(self, repo_root: Path) -> str | None:
        """Return the pointed session id; ``None`` when none is written."""
        try:
            return (self.root / self.repo_key(repo_root)).read_text().strip() or None
        except FileNotFoundError:
            return None

    def write(self, repo_root: Path, session_id: str) -> None:
        """Point *repo_root* at *session_id*, replacing any earlier pointer."""
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        target = self.root / self.repo_key(repo_root)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".current-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(session_id)
            Path(tmp).chmod(0o600)
            Path(tmp).replace(target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
