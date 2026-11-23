"""Choose the relay from the config files and locate the local relay's files.

The relay is the local filesystem relay unless a config file names another:

- ``type: local`` (or no ``relay`` section): the local relay, no network.
- ``type: demo``: the shared public NATS relay, with its bundled credentials.
- ``type: nats`` (or just ``url:``): a NATS server at ``relay.url``.

Config files (``config.yaml``, then ``config.local.yaml``) do not deep-merge
the relay: the highest file that sets ``type`` or ``url`` decides which relay
it is, and only a file at or above it can supply its credentials.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Self, cast

from biff._stdlib import biff_data_dir, get_repo_common_root
from biff.models import RelayAuth

DEMO_RELAY_URL = "tls://connect.ngs.global"
DEMO_CREDS_PATH = Path(str(importlib.resources.files("biff.data") / "demo.creds"))
RELAY_TYPES = ("local", "demo", "nats")

type ConfigLayer = tuple[str, dict[str, object]]
"""A config file's name (for messages) and its parsed content."""


@dataclass(frozen=True)
class RelaySetting:
    """What the config files say about the relay; a ``None`` URL is local."""

    url: str | None = None
    auth: RelayAuth | None = None
    tls_handshake_first: bool = False


@dataclass(frozen=True)
class RelayFile:
    """The ``relay`` section of one config file, checked on construction."""

    name: str
    section: dict[str, object]

    _AUTH_KEYS = ("token", "nkeys_seed", "user_credentials")
    _BOOL_KEYS = ("tls_handshake_first", "allow_env_override")
    _KEYS = frozenset(
        {"type", "url", "auth", "tls_handshake_first", "allow_env_override"}
        | set(_AUTH_KEYS)
    )

    @classmethod
    def parse(cls, name: str, raw: dict[str, object]) -> Self:
        """Check the relay section of the config dict *raw* read from *name*.

        Raises:
            SystemExit: The section is malformed or contradicts itself.
        """
        if "relays" in raw:
            raise SystemExit(f"{name}: unknown key 'relays'; did you mean 'relay'?")
        section: object = raw.get("relay")
        if section is not None and not isinstance(section, dict):
            kind = type(section).__name__
            raise SystemExit(f"{name}: 'relay' must be a mapping, not {kind}.")
        file = cls(name, cast("dict[str, object]", section or {}))
        file._check_keys()
        file._check_types()
        file._check_fit()
        return file

    def declares(self) -> bool:
        """Check whether this file sets ``type`` or ``url``."""
        return "type" in self.section or "url" in self.section

    def chooses(self) -> bool:
        """Check whether this file picks a relay (a ``type``, or a string ``url``)."""
        return self.section.get("type") is not None or isinstance(
            self.section.get("url"), str
        )

    def allows_env_override(self) -> bool:
        """Check whether this file opts in to ``BIFF_RELAY_*`` overrides."""
        return self.section.get("allow_env_override") is True

    def url_text(self) -> str | None:
        """Return ``relay.url``, or ``None`` when absent or empty."""
        url = self.section.get("url")
        return url if isinstance(url, str) and url else None

    def kind(self) -> str:
        """Return the relay type, which is ``nats`` when only a URL is given."""
        declared = self.section.get("type")
        if declared is None:
            return "nats" if self.url_text() else "local"
        if declared not in RELAY_TYPES:
            raise SystemExit(
                f"Unknown relay type {declared!r} in {self.name}.\n"
                f"Set type to one of: {', '.join(RELAY_TYPES)}."
            )
        return str(declared)

    def tls(self) -> bool | None:
        """Return ``tls_handshake_first``, or ``None`` when this file omits it."""
        value = self.section.get("tls_handshake_first")
        return value if isinstance(value, bool) else None

    def auth(self) -> RelayAuth | None:
        """Return this file's auth: at most one of token, nkeys_seed, credentials.

        Flat keys and a nested ``auth:`` mapping are both accepted; a flat key
        wins, and the nested mapping may spell the credentials file
        ``credentials``.
        """
        nested: object = self.section.get("auth")
        auth = cast("dict[str, object]", nested) if isinstance(nested, dict) else {}
        flat = {"user_credentials": auth.get("credentials"), **auth, **self.section}
        present = {
            key: value
            for key in self._AUTH_KEYS
            if isinstance(value := flat.get(key), str) and value
        }
        if len(present) > 1:
            raise SystemExit(
                f"Conflicting auth in relay section of {self.name}: "
                f"{', '.join(sorted(present))}\n"
                "Set at most one of 'token', 'nkeys_seed', or 'user_credentials'."
            )
        return RelayAuth(**present) if present else None

    def _check_keys(self) -> None:
        unknown = sorted(str(key) for key in self.section.keys() - self._KEYS)
        if unknown:
            raise SystemExit(
                f"{self.name}: unknown key(s) in relay section: "
                f"{', '.join(unknown)}.\nAllowed: {', '.join(sorted(self._KEYS))}."
            )

    def _check_types(self) -> None:
        url = self.section.get("url")
        if url is not None and not isinstance(url, str):
            raise SystemExit(f"{self.name}: relay.url must be a string, not {url!r}.")
        for key in self._BOOL_KEYS:
            if key in self.section and not isinstance(self.section[key], bool):
                raise SystemExit(f"{self.name}: relay.{key} must be true or false.")

    def _check_fit(self) -> None:
        """Reject keys that contradict this file's own ``type``."""
        kind = self.kind()
        if kind == "nats" and not self.url_text():
            raise SystemExit(
                f"{self.name}: relay type: nats requires relay.url "
                "(for example nats://host:4222)."
            )
        if "type" not in self.section or kind == "nats":
            return
        extras = [
            label
            for label, present in (
                ("url", self.url_text() is not None),
                ("auth", self.auth() is not None),
                ("tls_handshake_first", self.tls() is True),
            )
            if present
        ]
        if extras:
            raise SystemExit(
                f"{self.name}: relay type: {kind} cannot be combined with "
                f"{', '.join(extras)} in the same file."
            )


@dataclass(frozen=True)
class RelayLayers:
    """The relay sections of the config files, lowest precedence first."""

    files: tuple[RelayFile, ...]

    @classmethod
    def of(cls, layers: Sequence[ConfigLayer]) -> Self:
        """Check every file's relay section."""
        return cls(tuple(RelayFile.parse(name, raw) for name, raw in layers))

    def resolve(self) -> RelaySetting:
        """Return the relay these files choose.

        Raises:
            SystemExit: Credentials meet a relay that cannot take them (the
                local relay takes none, and the public demo relay must never
                receive your own), or no relay at all.
        """
        chosen = [i for i, file in enumerate(self.files) if file.chooses()]
        if not chosen:
            self._reject_orphan_auth()
            return RelaySetting()
        above = self.files[chosen[-1] :]
        kind = above[0].kind()
        # Credentials and TLS mode come from the highest file that sets them,
        # at or above the file that chose the relay.
        auth = next((a for file in reversed(above) if (a := file.auth())), None)
        tls = self._tls(above)
        self._check_fit(kind, has_extras=auth is not None or tls)
        url = DEMO_RELAY_URL if kind == "demo" else above[0].url_text()
        if url == DEMO_RELAY_URL and auth is None:
            auth = RelayAuth(user_credentials=str(DEMO_CREDS_PATH))
        return RelaySetting(url, auth, tls)

    def _reject_orphan_auth(self) -> None:
        if any(file.auth() for file in self.files):
            raise SystemExit("relay credentials are set but no relay type or url is.")

    @staticmethod
    def _tls(files: Sequence[RelayFile]) -> bool:
        """Return the last ``tls_handshake_first`` any of *files* sets."""
        values = [tls for file in files if (tls := file.tls()) is not None]
        return values[-1] if values else False

    @staticmethod
    def _check_fit(kind: str, *, has_extras: bool) -> None:
        if kind == "nats" or not has_extras:
            return
        reason = (
            "the public demo relay must never receive your own credentials"
            if kind == "demo"
            else "the local relay takes none"
        )
        raise SystemExit(
            f"relay type: {kind} cannot use credentials or tls_handshake_first; "
            f"{reason}."
        )


@dataclass(frozen=True)
class RelayKey:
    """The directory name that keys a git remote's local relay, and why."""

    name: str
    reason: str

    _PROXY = re.compile(
        r"https://git-manager\.devin\.ai/proxy/(?P<host>github\.com)/(?P<path>.+)"
    )
    _URL = re.compile(
        r"[a-z][a-z0-9+.-]*://(?:[^/@]*@)?(?P<host>[^/:@]+)(?::\d+)?/(?P<path>.+)",
        re.IGNORECASE,
    )
    _SCP = re.compile(r"(?:[^@/]+@)?(?P<host>[^:/@]+):(?P<path>[^/].*)")

    @classmethod
    def of(cls, url: str, base: Path | None = None) -> Self:
        """Return the key for the git remote *url*, as seen from *base*.

        The key is built from the normalized identity (lowercase host and
        ``owner/repo``, no trailing ``/`` or ``.git``), so ssh and https forms
        of one remote agree and different identities never do.  A remote that
        is not ``host/owner/repo`` (a GitLab subgroup) is keyed by its
        normalized URL.  A filesystem remote (a path or ``file://`` URL, absolute
        or relative to *base*) is identified by the absolute path it resolves
        to, with its case preserved.
        """
        text = url.strip().rstrip("/").removesuffix(".git").rstrip("/").lower()
        match = (
            cls._PROXY.fullmatch(text)
            or cls._URL.fullmatch(text)
            or cls._SCP.fullmatch(text)
        )
        if match is None or text.startswith("file:"):
            path = (base or Path()) / url.strip().removeprefix("file://")
            resolved = path.expanduser().resolve()
            return cls(
                f"remote-{cls._digest(str(resolved))}", f"remote path {resolved}"
            )
        where = f"{match['host']}/{match['path'].strip('/')}"
        parts = where.split("/")[1:]
        if len(parts) != 2 or not all(parts):
            return cls(f"remote-{cls._digest(where)}", f"remote URL hash of {where}")
        slug = cls._slug("__".join(parts))
        return cls(f"{slug}-{cls._digest(where)}", f"remote slug of {where}")

    @classmethod
    def for_path(cls, common_root: Path) -> Self:
        """Return the key for a repo with no remote: its resolved common root."""
        root = common_root.resolve()
        name = f"{cls._slug(root.name)}-{cls._digest(str(root))}"
        return cls(name, f"no remote: keyed by the repo's path ({root})")

    @staticmethod
    def _digest(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:8]

    @staticmethod
    def _slug(text: str) -> str:
        """Return *text* lowercased with anything outside ``[a-z0-9_-]`` as ``-``."""
        slug = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-")
        return slug[:60] or "repo"


@dataclass(frozen=True)
class RelayLocation:
    """A repo's local relay directory and why it is that directory."""

    path: Path
    reason: str

    @classmethod
    def of(cls, repo_root: Path) -> Self:
        """Return the local relay directory for the repo at *repo_root*.

        Every clone and worktree of one repo lands on the same directory, and
        two different repos never do.
        """
        url = cls._remote_url(repo_root)
        common = Path(get_repo_common_root(str(repo_root)) or repo_root)
        key = RelayKey.of(url, common) if url else RelayKey.for_path(common)
        return cls(biff_data_dir() / "relay" / key.name, key.reason)

    @staticmethod
    def _remote_url(repo_root: Path) -> str | None:
        """Return the URL of ``origin``, or of the only remote when there is one.

        ``None`` when git is unavailable, there is no remote, or several remotes
        exist and none is ``origin``.  The URL is read as configured, before any
        ``insteadOf`` rewriting, so every clone of a remote agrees on it.
        """
        try:
            result = subprocess.run(
                ["git", "config", "--get-regexp", r"^remote\..*\.url$"],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
                cwd=repo_root,
            )
        except OSError:
            return None
        urls: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, _, url = line.partition(" ")
            urls[key.removeprefix("remote.").removesuffix(".url")] = url.strip()
        if "origin" in urls:
            return urls["origin"]
        return next(iter(urls.values())) if len(urls) == 1 else None
