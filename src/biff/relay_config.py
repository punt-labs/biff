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

import importlib.resources
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Self, cast

from biff._stdlib import get_repo_common_root
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
    demo: bool = False


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
        return RelaySetting(url, auth, tls, demo=kind == "demo")

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
class RelayLocation:
    """A repo's local relay directory and why it is that directory."""

    path: Path
    reason: str

    @classmethod
    def of(cls, repo_root: Path) -> Self:
        """Return the local relay directory for the repo at *repo_root*.

        The relay is private runtime state of the repo's owning store, its main
        checkout: ``<store>/.punt-labs/local/biff/relay/``.  A linked worktree
        shares its main checkout's relay; two separate checkouts never share.
        """
        store = Path(get_repo_common_root(str(repo_root)) or repo_root).resolve()
        path = store / ".punt-labs" / "local" / "biff" / "relay"
        return cls(path, f"private to the repo's main checkout ({store})")

    @staticmethod
    def ensure(data_dir: Path) -> None:
        """Create *data_dir* 0700, tightening it if it already exists.

        In a repo's private zone (``.punt-labs/local/biff/relay``) biff's own
        ``local/biff`` directory is tightened too and holds a ``.gitignore`` of
        ``*``, so relay state never enters shared history; the zone must not
        pass through a symlink.  Any other parent of a custom data directory is
        left as it is.
        """
        in_zone = data_dir.parts[-4:] == (".punt-labs", "local", "biff", "relay")
        if in_zone:
            RelayLocation._refuse_symlinks(data_dir)
        for path in (data_dir.parent, data_dir) if in_zone else (data_dir,):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)
        if in_zone:
            RelayLocation._ignore_everything(data_dir.parent)

    @staticmethod
    def _refuse_symlinks(data_dir: Path) -> None:
        """Raise ``OSError`` when the zone path passes through a symlink."""
        for path in (data_dir, *data_dir.parents[:3]):
            if path.is_symlink():
                msg = f"refusing a relay directory reached through symlink {path}"
                raise OSError(msg)

    @staticmethod
    def _ignore_everything(owned: Path) -> None:
        """Keep a ``.gitignore`` of ``*`` in biff's own ``local/biff``."""
        ignore = owned / ".gitignore"
        if not ignore.is_file() or ignore.read_text() != "*\n":
            ignore.write_text("*\n")
