"""The relay settings a process was started with, as digests of their values."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Final, Literal, Self, final

if TYPE_CHECKING:
    from collections.abc import Mapping

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

    __slots__ = ("_digests",)

    _digests: Mapping[RelayVariable, str]

    def __new__(cls, digests: Mapping[RelayVariable, str]) -> Self:
        """Create the seed that holds *digests*, keyed by variable name."""
        self = super().__new__(cls)
        self._digests = dict(digests)
        return self

    @classmethod
    def of(cls, environ: Mapping[str, str]) -> Self:
        """Return the seed of *environ*: one digest per relay variable it sets."""
        return cls(
            {
                name: hashlib.sha256(environ[name].encode()).hexdigest()
                for name in RELAY_VARIABLES
                if environ.get(name)
            }
        )

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

    def conflicts_with(self, seeded: RelaySeed) -> tuple[RelayVariable, ...]:
        """Return the variables set here that *seeded* does not set to the same value.

        A variable only *seeded* sets is no conflict: a client that carries none
        of its own is served by the settings biffd was started with.
        """
        return tuple(
            name
            for name in self.names()
            if self._digests[name] != seeded.digest_of(name)
        )

    def admit(self, client: RelaySeed) -> None:
        """Accept *client*, or say which of its variables differ from this seed.

        Raises:
            RelayConflictError: *client* sets a variable this seed lacks or sets to
                another value.  The message names variables, never values.
        """
        differing = client.conflicts_with(self)
        if not differing:
            return
        names = ", ".join(differing)
        started = (
            f"biffd was started without {names}"
            if not any(self.digest_of(name) for name in differing)
            else f"{names} differ from the ones biffd was started with"
        )
        msg = (
            f"{started}; biffd serves one relay setting for all its sessions. "
            f"Unset {names} here, or stop biffd and run `biff daemon serve` from "
            "an environment that sets them"
        )
        raise RelayConflictError(msg)

    def describe(self) -> str:
        """Return the variable names for a log line, or ``none``."""
        return ", ".join(self.names()) or "none"

    def digest_of(self, name: RelayVariable) -> str:
        """Return the digest of *name*'s value, or ``""`` when it is unset."""
        return self._digests.get(name, "")

    def __bool__(self) -> bool:
        """Return whether any relay variable is set."""
        return bool(self._digests)

    def __repr__(self) -> str:
        """Name the variables; the digests stay out of any log line."""
        return f"RelaySeed({', '.join(self.names())})"


NO_RELAY_SETTINGS: Final = RelaySeed({})
"""The seed of a process that sets no relay variable."""
