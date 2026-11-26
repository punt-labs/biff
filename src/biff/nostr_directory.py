"""Public Nostr keys and attestations, kept in an ethos ext namespace."""

from __future__ import annotations

import asyncio
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
LOOKUP_FAILED = "?"
_HANDLE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_ETHOS_TIMEOUT_SECONDS = 15
_NOT_SET_MARK = f'namespace "{NAMESPACE}" not found'


class ExtStore(Protocol):
    """Somewhere an identity's ``nostr`` ext namespace lives."""

    def get(self, handle: str) -> dict[str, str]:
        """Return the namespace's fields; empty only when it is truly unset.

        Raises ``OSError`` for any other failure, so "no key" never means
        "could not ask".
        """
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
        done = self._run(
            self._ethos, self._cwd, ["ext", "get", handle, NAMESPACE, "--json"]
        )
        if done.returncode != 0:
            if _NOT_SET_MARK in done.stderr:
                return {}
            msg = (
                f"ethos ext get {handle} {NAMESPACE} failed "
                f"(exit {done.returncode}): {done.stderr.strip()}"
            )
            raise OSError(msg)
        try:
            raw: object = json.loads(done.stdout)
        except json.JSONDecodeError as exc:
            msg = f"ethos ext get {handle} {NAMESPACE} returned unreadable output"
            raise OSError(msg) from exc
        if not isinstance(raw, dict):
            msg = f"ethos ext get {handle} {NAMESPACE} did not return an object"
            raise OSError(msg)
        return {str(k): str(v) for k, v in cast("dict[str, object]", raw).items()}

    def set(self, handle: str, key: str, value: str) -> None:
        """Set one field through ``ethos ext set``."""
        done = self._run(
            self._ethos, self._cwd, ["ext", "set", handle, NAMESPACE, key, value]
        )
        if done.returncode != 0:
            reason = done.stderr.strip()
            msg = f"ethos ext set {handle} {NAMESPACE} {key} failed: {reason}"
            raise OSError(msg)

    @staticmethod
    def _run(
        ethos: str, cwd: Path | None, args: list[str]
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [ethos, *args],
                capture_output=True,
                text=True,
                check=False,
                cwd=cwd,
                timeout=_ETHOS_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            msg = f"ethos {' '.join(args[:2])} timed out after {exc.timeout:g}s"
            raise OSError(msg) from exc


@final
class NostrDirectory:
    """Reads and writes the ``nostr`` namespace of an identity's ext.

    With ethos present, the namespace is ethos's own ext store and nothing is
    written anywhere else.  Without ethos, the same namespace lives in one
    local YAML file mapping each handle to its namespaces.  Only public values
    go here: ext values can reach model context.
    """

    __slots__ = ("_ext", "_local")

    _ext: ExtStore | None
    _local: Path

    def __new__(cls, ext: ExtStore | None, local_file: Path) -> Self:
        """Use *ext* when given; otherwise keep the namespace in *local_file*."""
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

    def publish(self, handle: str, fields: Mapping[str, str]) -> None:
        """Merge *fields* into *handle*'s ``nostr`` namespace.

        Raises ``OSError`` when the namespace was not written.  With ethos
        present a refusal is final: the local file is not a second home.
        """
        self._check_handle(handle)
        if self._ext is not None:
            self._publish_through(self._ext, handle, fields)
            return
        book = self._read_mapping(self._local, strict=True)
        merged = {**self._local_fields(book, handle), **fields}
        book[handle] = {NAMESPACE: merged}
        self._write(self._local, book)

    def fields(self, handle: str) -> dict[str, str]:
        """Return *handle*'s published fields; empty when it has none.

        Raises ``OSError`` when ethos cannot be asked.  An unreadable local
        file degrades to no fields, with a warning that names it.
        """
        self._check_handle(handle)
        local = self._local_fields(self._read_mapping(self._local), handle)
        return {**local, **(self._ext.get(handle) if self._ext else {})}

    def npub(self, handle: str) -> str | None:
        """Return *handle*'s npub; ``None`` is a handle with no published key."""
        return self.fields(handle).get("npub")

    @staticmethod
    def _publish_through(ext: ExtStore, handle: str, fields: Mapping[str, str]) -> None:
        written: list[str] = []
        for key, value in fields.items():
            try:
                ext.set(handle, key, value)
            except OSError as exc:
                partial = f"; already written: {', '.join(written)}" if written else ""
                msg = f"published nowhere: {exc}{partial}"
                raise OSError(msg) from exc
            written.append(key)

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
    def _read_mapping(path: Path, *, strict: bool = False) -> dict[str, object]:
        """Return the YAML mapping at *path*; empty when the file is absent.

        An unreadable file or one that is not a mapping is a warning and an
        empty result, or with *strict* an ``OSError``: a writer must never
        replace what it could not read.
        """
        try:
            raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            return NostrDirectory._unreadable(path, str(exc), strict=strict)
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            return NostrDirectory._unreadable(
                path, "it is not a mapping", strict=strict
            )
        return cast("dict[str, object]", raw)

    @staticmethod
    def _unreadable(path: Path, why: str, *, strict: bool) -> dict[str, object]:
        if strict:
            msg = f"cannot update {path}: {why}; fix or move the file and retry"
            raise OSError(msg)
        logger.warning("Cannot read %s: %s", path, why)
        return {}

    @staticmethod
    def _write(path: Path, data: Mapping[str, object]) -> None:
        """Replace *path* with *data* atomically."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(dict(data), handle, sort_keys=True)
        Path(tmp).chmod(0o644)
        Path(tmp).replace(path)


@final
class SessionKeys:
    """The published npub of each user a ``who`` table lists.

    A user whose lookup failed maps to :data:`LOOKUP_FAILED`; a user with no
    key, or a name that is not a handle, is left out, so a blank cell means
    "nothing published" and ``?`` means "could not find out".
    """

    __slots__ = ("_directory",)

    _directory: NostrDirectory

    def __new__(cls, directory: NostrDirectory) -> Self:
        """Look keys up in *directory*."""
        self = super().__new__(cls)
        self._directory = directory
        return self

    @classmethod
    def for_repo(cls, repo_root: Path | None) -> Self:
        """Return the keys of a session in *repo_root* on this machine."""
        return cls(NostrDirectory.for_repo(repo_root))

    async def lookup(self, sessions: Iterable[UserSession]) -> dict[str, str]:
        """Return the npub of each session user that has one.

        Runs off the event loop: it may start an ``ethos`` process per user.
        """
        users = tuple(dict.fromkeys(s.user for s in sessions))
        return await asyncio.to_thread(self._collect, self._directory, users)

    @staticmethod
    def _collect(directory: NostrDirectory, users: tuple[str, ...]) -> dict[str, str]:
        found: dict[str, str] = {}
        for handle in users:
            if not _HANDLE_RE.fullmatch(handle):
                logger.debug("who user is not a valid handle; no key lookup")
                continue
            try:
                npub = directory.npub(handle)
            except OSError as exc:
                logger.warning("key lookup for %s failed: %s", handle, exc)
                found[handle] = LOOKUP_FAILED
                continue
            if npub:
                found[handle] = npub
        return found
