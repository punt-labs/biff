"""The relay settings a process was started with, as digests of their values."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Self, final

if TYPE_CHECKING:
    from collections.abc import Mapping

    from biff.models import RelayAuth

logger = logging.getLogger(__name__)

__all__ = [
    "NO_RELAY_SETTINGS",
    "RELAY_VARIABLES",
    "RelayConflictError",
    "RelaySeed",
    "RelayVariable",
]

type RelayVariable = Literal[
    "BIFF_RELAY_NKEYS_SEED",
    "BIFF_RELAY_TLS_HANDSHAKE_FIRST",
    "BIFF_RELAY_TOKEN",
    "BIFF_RELAY_URL",
    "BIFF_RELAY_USER_CREDENTIALS",
]

RELAY_VARIABLES: Final[tuple[RelayVariable, ...]] = (
    "BIFF_RELAY_NKEYS_SEED",
    "BIFF_RELAY_TLS_HANDSHAKE_FIRST",
    "BIFF_RELAY_TOKEN",
    "BIFF_RELAY_URL",
    "BIFF_RELAY_USER_CREDENTIALS",
)
"""The environment variables that choose a relay; sorted, so messages are stable."""


_TLS_TRUE_VALUES: Final = frozenset({"1", "true", "yes"})
_TLS_FALSE_VALUES: Final = frozenset({"0", "false", "no"})

_HOST: Final = re.compile(r"[a-z0-9.\-:\[\]]+")
_SCHEME: Final = re.compile(r"[a-z][a-z0-9+.\-]*")

_CREDENTIALS: Final[tuple[RelayVariable, ...]] = (
    "BIFF_RELAY_NKEYS_SEED",
    "BIFF_RELAY_TOKEN",
    "BIFF_RELAY_USER_CREDENTIALS",
)


_CREDENTIAL_PATHS: Final[tuple[RelayVariable, ...]] = (
    "BIFF_RELAY_NKEYS_SEED",
    "BIFF_RELAY_USER_CREDENTIALS",
)


class RelayConflictError(ValueError):
    """A client's relay settings differ from the ones biffd was started with."""


@final
class RelaySeed:
    """Which ``BIFF_RELAY_*`` variables a process sets, each as a digest of its value.

    biffd resolves every session in its own process, from the environment it was
    started in.  A client compares its own environment with biffd's by digest, so
    neither side prints, logs or sends a secret to learn that they differ.  An
    empty value counts as unset, as the relay configuration reads it.
    """

    __slots__ = ("_digests", "_files", "_relay", "_settings")

    _digests: Mapping[RelayVariable, str]
    # ``scheme://host[:port]`` of the relay, or ``unparseable``: what may be shown.
    _relay: str
    # One keyed hash over all the relay settings, the whole URL and every
    # credential included, so two seeds that differ anywhere differ here and no
    # value or bare digest of one is kept.  Empty when no key was given.
    _settings: str
    # (device, inode) of each credential file biffd was started with: the file
    # itself, whatever name a later session gives it.  Never printed.
    _files: frozenset[tuple[int, int]]

    def __new__(
        cls,
        digests: Mapping[RelayVariable, str],
        relay: str = "",
        files: frozenset[tuple[int, int]] = frozenset(),
        settings: str = "",
    ) -> Self:
        """Create the seed that holds *digests*, keyed by variable name."""
        self = super().__new__(cls)
        self._digests = dict(digests)
        self._relay = relay
        self._settings = settings
        self._files = files
        return self

    @classmethod
    def of(
        cls,
        environ: Mapping[str, str],
        *,
        files: frozenset[tuple[int, int]] = frozenset(),
        key: bytes = b"",
    ) -> Self:
        """Return the seed of *environ*: one digest per relay variable it sets.

        *files* are the identities of the credential files it names and *key* keys
        the hash of all its settings; only biffd records them.
        """
        digests: dict[RelayVariable, str] = {
            name: cls._digest(environ[name], name)
            for name in RELAY_VARIABLES
            if cls._is_setting(name, environ.get(name, ""))
        }
        return cls(
            digests,
            relay=cls._display_of(environ.get("BIFF_RELAY_URL", "")),
            files=files,
            settings=cls._settings_of(environ, key) if key and digests else "",
        )

    @staticmethod
    def _display_of(url: str) -> str:
        """Return *url* as shown: ``scheme://host[:port]``, or ``unparseable``.

        Built from the text after the last ``@`` only, so userinfo cannot reach it.
        A ``/``, ``?`` or ``#`` before that ``@`` makes the split ambiguous, and a
        host or scheme with any character outside the plain set is not shown, so a
        control character cannot reach a log or a terminal.  Empty for no URL.
        """
        scheme, sep, rest = url.strip().partition("://")
        if not sep:
            return "unparseable" if url.strip() else ""
        head, at, tail = rest.rpartition("@")
        host = re.split("[/?#]", tail, maxsplit=1)[0].casefold()
        if (at and re.search("[/?#]", head)) or not _HOST.fullmatch(host):
            return "unparseable"
        if not _SCHEME.fullmatch(scheme.casefold()):
            return "unparseable"
        return f"{scheme.casefold()}://{host}"

    @staticmethod
    def _normalised(url: str) -> str:
        """Return *url* with its scheme and host case folded and no final ``/``."""
        scheme, sep, rest = url.strip().partition("://")
        if not sep:
            return url.strip()
        userinfo, at, tail = rest.rpartition("@")
        cut = re.search("[/?#]", tail)
        host, after = (tail[: cut.start()], tail[cut.start() :]) if cut else (tail, "")
        folded = f"{scheme.casefold()}://{userinfo}{at}{host.casefold()}{after}"
        return folded.rstrip("/")

    @classmethod
    def _settings_of(cls, environ: Mapping[str, str], key: bytes) -> str:
        """Return one keyed hash of every relay setting in *environ*.

        The whole URL, userinfo and path included, the token, the resolved paths of
        the credential files and the parsed TLS flag all go into it, so a difference
        in any of them is a difference here.  The key is random and private to biffd:
        a low-entropy token cannot be guessed from what biffd keeps.
        """
        fields = [
            f"url={cls._normalised(environ.get('BIFF_RELAY_URL', ''))}",
            f"token={environ.get('BIFF_RELAY_TOKEN', '')}",
            f"tls={cls.tls_value(environ.get('BIFF_RELAY_TLS_HANDSHAKE_FIRST', ''))}",
            *(
                f"{name}={os.path.realpath(environ[name])}"
                for name in _CREDENTIAL_PATHS
                if environ.get(name)
            ),
        ]
        return hmac.new(key, "\0".join(fields).encode(), "sha256").hexdigest()

    @staticmethod
    def _identity(name: RelayVariable, path: str) -> tuple[int, int] | None:
        """Return the (device, inode) of the file *path* names now, if it has one."""
        try:
            status = Path(path).stat()
        except OSError:
            logger.warning(
                "cannot read the file %s names; it is not the seeded one", name
            )
            return None
        return (status.st_dev, status.st_ino)

    @staticmethod
    def tls_value(raw: str) -> bool | None:
        """Read ``BIFF_RELAY_TLS_HANDSHAKE_FIRST``; ``None`` when it does nothing.

        The one reading the relay configuration and the seed share: case and
        padding do not matter, and a value that is neither a yes nor a no is
        ignored.
        """
        normalized = raw.strip().casefold()
        if normalized in _TLS_TRUE_VALUES:
            return True
        if normalized in _TLS_FALSE_VALUES:
            return False
        return None

    @classmethod
    def _is_setting(cls, name: RelayVariable, raw: str) -> bool:
        """Return whether *raw* sets *name* as the relay configuration reads it."""
        if name == "BIFF_RELAY_TLS_HANDSHAKE_FIRST":
            return cls.tls_value(raw) is not None
        return bool(raw)

    @classmethod
    def _digest(cls, value: str, name: RelayVariable) -> str:
        """Return the SHA-256 of *value* in the form the configuration reads."""
        if name == "BIFF_RELAY_URL":
            value = value.strip().rstrip("/").casefold()
        elif name == "BIFF_RELAY_TLS_HANDSHAKE_FIRST":
            value = str(cls.tls_value(value))
        elif name in _CREDENTIAL_PATHS:
            # One file has many spellings (relative, ``..``, a symlink), and a
            # relative one means the file under the cwd of the process that reads it.
            value = os.path.realpath(value)
        return hashlib.sha256(value.encode()).hexdigest()

    @classmethod
    def from_wire(cls, digests: Mapping[RelayVariable, str]) -> Self:
        """Return the seed a hello carried."""
        return cls(digests)

    def to_wire(self) -> dict[RelayVariable, str]:
        """Return the digests for a hello."""
        return dict(self._digests)

    def names(self) -> tuple[RelayVariable, ...]:
        """Return the variables this seed sets, sorted."""
        return tuple(name for name in RELAY_VARIABLES if name in self._digests)

    def differing_from(self, other: RelaySeed) -> tuple[RelayVariable, ...]:
        """Return the variables set in one seed and not set the same in the other."""
        return tuple(
            name
            for name in RELAY_VARIABLES
            if self._digests.get(name, "") != other.digest_of(name)
        )

    def admit(self, client: RelaySeed) -> None:
        """Accept *client* only if its settings equal this seed's, in both directions.

        A client that sets less than biffd was started with would otherwise be
        served by relay settings it never chose, and one that sets more would be
        ignored.

        Raises:
            RelayConflictError: A relay variable is set in one and not set the same
                in the other.  The message names variables, never values.
        """
        differing = client.differing_from(self)
        if not differing:
            return
        msg = (
            f"this process's relay settings must equal biffd's: biffd was started "
            f"with {self.listed()}, this process sets "
            f"{client.listed()} (differing: {', '.join(differing)}). Set "
            "the same environment here, or stop biffd and let it start again from "
            "this environment"
        )
        raise RelayConflictError(msg)

    def listed(self) -> str:
        """Return the variable names, comma separated, or ``none``."""
        return (
            ", ".join(name for name in RELAY_VARIABLES if name in self._digests)
            or "none"
        )

    @classmethod
    def for_daemon(cls, environ: Mapping[str, str], key: bytes) -> Self:
        """Return the seed biffd starts with, or say why it cannot.

        biffd sends the credentials it was started with only to the relay it was
        started with, so it needs one URL and at most one credential.  A key or
        credentials file is read by biffd from its own directory, so it must be an
        absolute path to a file that exists.  *key* keys the hash that tells one
        token from another in what biffd keeps.

        Raises:
            RelayConflictError: A credential has no ``BIFF_RELAY_URL``, there is
                more than one credential, or a credentials path is not an absolute
                path to an existing file.  The message names variables only.
        """
        cls.of(environ).require_whole()
        files = {
            identity
            for name in _CREDENTIAL_PATHS
            if (raw := environ.get(name, ""))
            and (identity := cls._require_file(name, raw)) is not None
        }
        return cls.of(environ, files=frozenset(files), key=key)

    def require_whole(self) -> None:
        """Refuse a seed with a credential and no URL, or with two credentials.

        Raises:
            RelayConflictError: The seed is not one URL and at most one credential.
        """
        held = [name for name in _CREDENTIALS if name in self._digests]
        if len(held) > 1:
            msg = f"{', '.join(held)} are all set; biffd takes at most one of them"
            raise RelayConflictError(msg)
        if held and "BIFF_RELAY_URL" not in self._digests:
            msg = (
                f"{held[0]} is set but BIFF_RELAY_URL is not; biffd sends its "
                "credentials only to the relay it was started with. Set "
                "BIFF_RELAY_URL too, or unset it"
            )
            raise RelayConflictError(msg)

    @classmethod
    def _require_file(cls, name: RelayVariable, raw: str) -> tuple[int, int] | None:
        """Return the identity of the credentials file *raw* names, or refuse it."""
        if not (Path(raw).is_absolute() and Path(raw).is_file()):
            msg = f"{name} must be an absolute path to a file that exists"
            raise RelayConflictError(msg)
        return cls._identity(name, raw)

    def require_credentials_home(
        self, relay_url: str | None, auth: RelayAuth | None
    ) -> None:
        """Refuse a session that would send the seeded credentials elsewhere.

        Raises:
            RelayConflictError: *auth* holds a credential this seed holds and
                *relay_url* is not this seed's ``BIFF_RELAY_URL``.
        """
        if auth is None or not self._holds_seeded(auth):
            return
        if self._digest(relay_url or "", "BIFF_RELAY_URL") != self._digests.get(
            "BIFF_RELAY_URL"
        ):
            msg = (
                "this session's relay is not the BIFF_RELAY_URL biffd was started "
                "with, the only relay it sends its BIFF_RELAY_* credentials to"
            )
            raise RelayConflictError(msg)

    def _holds_seeded(self, auth: RelayAuth) -> bool:
        """Return whether *auth* is a credential this seed holds, by value or file."""
        seeded = {self._digests[name] for name in _CREDENTIALS if name in self._digests}
        fields: tuple[tuple[RelayVariable, str | None], ...] = (
            ("BIFF_RELAY_TOKEN", auth.token),
            ("BIFF_RELAY_NKEYS_SEED", auth.nkeys_seed),
            ("BIFF_RELAY_USER_CREDENTIALS", auth.user_credentials),
        )
        if {self._digest(value, name) for name, value in fields if value} & seeded:
            return True
        # The file itself, not only its path now: a hardlink has another name.
        return bool(self._files) and any(
            self._identity(name, value) in self._files
            for name, value in fields
            if value and name in _CREDENTIAL_PATHS
        )

    def describe(self) -> str:
        """Say which variables are set and the relay's host, never a value."""
        names = self.listed()
        return f"{names} (relay {self._relay})" if self._relay else names

    def identity(self) -> str:
        """Say which relay settings this seed names, for biffd to keep.

        What biffd compares across a restart: the variable names, the relay's scheme
        and host, and one keyed hash of all the settings.  It holds no secret and no
        bare digest of one.
        """
        described = self.describe()
        return (
            f"{described}; settings {self._settings}" if self._settings else described
        )

    @staticmethod
    def explain_change(previous: str, current: str) -> str:
        """Say how two identities differ, repeating no value.

        Names both hosts when the names or the host differ, and otherwise says that
        the credentials or the path do.  Empty when they are the same.
        """
        before, _, old = previous.partition("; settings ")
        after, _, new = current.partition("; settings ")
        if before != after:
            return f"started with {after}; the last start had {before}"
        return "credentials or path differs" if old != new else ""

    def digest_of(self, name: RelayVariable) -> str:
        """Return the digest of *name*'s value, or ``""`` when it is unset."""
        return self._digests.get(name, "")

    def __bool__(self) -> bool:
        """Return whether any relay variable is set."""
        return bool(self._digests)

    def __repr__(self) -> str:
        """Name the variables; the digests stay out of any log line."""
        return f"RelaySeed({', '.join(sorted(self._digests))})"


NO_RELAY_SETTINGS: Final = RelaySeed({})
"""The seed of a process that sets no relay variable."""
