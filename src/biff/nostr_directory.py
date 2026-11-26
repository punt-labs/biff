"""Public Nostr keys and attestations, kept in an ethos ext namespace."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Protocol, Self, cast, final

import yaml

from biff.models import UserSession

logger = logging.getLogger(__name__)

NAMESPACE = "nostr"
_HANDLE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_ETHOS_TIMEOUT_SECONDS = 15


class ExtStore(Protocol):
    """Somewhere an identity's ``nostr`` ext namespace lives."""

    def get(self, handle: str) -> dict[str, str]:
        """Return the namespace's fields; empty when it has none."""
        ...

    def set(self, handle: str, key: str, value: str) -> None:
        """Set one field, raising ``OSError`` when the store refuses."""
        ...


@final
class EthosExt:
    """The ``nostr`` namespace, read and written through the ``ethos`` command.

    Ethos decides where an ext value is stored: the repository's tracked
    identity tree is not biff's to write.
    """

    __slots__ = ("_cwd", "_ethos")

    _ethos: str
    _cwd: Path | None

    def __new__(cls, ethos: str, cwd: Path | None) -> Self:
        """Run the *ethos* executable from *cwd*, the repository it resolves."""
        self = super().__new__(cls)
        self._ethos = ethos
        self._cwd = cwd
        return self

    def get(self, handle: str) -> dict[str, str]:
        """Return the fields of *handle*'s namespace; empty when it is unset."""
        done = self._run(["ext", "get", handle, NAMESPACE, "--json"])
        if done.returncode != 0:
            return {}
        try:
            raw: object = json.loads(done.stdout)
        except json.JSONDecodeError:
            logger.warning("ethos ext get returned unreadable output for %s", handle)
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in cast("dict[str, object]", raw).items()}

    def set(self, handle: str, key: str, value: str) -> None:
        """Set one field through ``ethos ext set``."""
        done = self._run(["ext", "set", handle, NAMESPACE, key, value])
        if done.returncode != 0:
            reason = done.stderr.strip()
            msg = f"ethos ext set {handle} {NAMESPACE} {key} failed: {reason}"
            raise OSError(msg)

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self._ethos, *args],
            capture_output=True,
            text=True,
            check=False,
            cwd=self._cwd,
            timeout=_ETHOS_TIMEOUT_SECONDS,
        )


@final
class NostrDirectory:
    """Reads and writes the ``nostr`` namespace of an identity's ext.

    With ethos present, the namespace is ethos's own ext store.  Without it,
    or for a handle ethos refuses, the same namespace lives in one local YAML
    file mapping each handle to its namespaces.  Only public values go here:
    ext values can reach model context.
    """

    __slots__ = ("_ext", "_local")

    _ext: ExtStore | None
    _local: Path

    def __new__(cls, ext: ExtStore | None, local_file: Path) -> Self:
        """Use *ext* when given; keep the fallback in *local_file*."""
        self = super().__new__(cls)
        self._ext = ext
        self._local = local_file
        return self

    @classmethod
    def for_repo(cls, repo_root: Path | None) -> Self:
        """Return the directory for a session in *repo_root* on this machine."""
        ethos = shutil.which("ethos")
        ext = EthosExt(ethos, repo_root) if ethos else None
        return cls(ext, Path.home() / ".punt-labs" / "biff" / "nostr.yaml")

    def session_npubs(self, sessions: Iterable[UserSession]) -> dict[str, str]:
        """Return the npub of each session user that has one."""
        found: dict[str, str] = {}
        for handle in dict.fromkeys(s.user for s in sessions):
            try:
                npub = self.npub(handle)
            except ValueError:
                continue  # a session user that is not a valid handle has no key
            if npub:
                found[handle] = npub
        return found

    def publish(self, handle: str, fields: Mapping[str, str]) -> None:
        """Merge *fields* into *handle*'s ``nostr`` namespace."""
        self._check_handle(handle)
        if self._ext is not None:
            try:
                for key, value in fields.items():
                    self._ext.set(handle, key, value)
            except OSError as exc:
                logger.warning("%s; keeping the key in %s", exc, self._local)
            else:
                return
        book = self._read_mapping(self._local)
        merged = {**self._local_fields(book, handle), **fields}
        book[handle] = {NAMESPACE: merged}
        self._write(self._local, book)

    def fields(self, handle: str) -> dict[str, str]:
        """Return *handle*'s published fields; empty when it has none."""
        self._check_handle(handle)
        local = self._local_fields(self._read_mapping(self._local), handle)
        return {**local, **(self._ext.get(handle) if self._ext else {})}

    def npub(self, handle: str) -> str | None:
        """Return *handle*'s npub; ``None`` is a handle with no published key."""
        return self.fields(handle).get("npub")

    @staticmethod
    def _check_handle(handle: str) -> None:
        if not _HANDLE_RE.fullmatch(handle):
            msg = f"invalid handle {handle!r}: use lowercase letters, digits, - and _"
            raise ValueError(msg)

    @staticmethod
    def _local_fields(book: Mapping[str, object], handle: str) -> dict[str, str]:
        entry = book.get(handle)
        if not isinstance(entry, dict):
            return {}
        namespace = cast("dict[str, object]", entry).get(NAMESPACE)
        if not isinstance(namespace, dict):
            return {}
        return {str(k): str(v) for k, v in cast("dict[str, object]", namespace).items()}

    @staticmethod
    def _read_mapping(path: Path) -> dict[str, object]:
        """Return the YAML mapping at *path*; empty when absent or malformed."""
        try:
            raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            logger.warning("Cannot read %s: %s", path, exc)
            return {}
        return cast("dict[str, object]", raw) if isinstance(raw, dict) else {}

    @staticmethod
    def _write(path: Path, data: Mapping[str, object]) -> None:
        """Replace *path* with *data* atomically."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(dict(data), handle, sort_keys=True)
        Path(tmp).chmod(0o644)
        Path(tmp).replace(path)
