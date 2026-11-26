"""What a client carries from its own environment into its hello."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, final

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["ClientEnvironment", "UnsupportedOverrideError"]

_RELAY_OVERRIDES: Final = (
    "BIFF_RELAY_NKEYS_SEED",
    "BIFF_RELAY_TLS_HANDSHAKE_FIRST",
    "BIFF_RELAY_TOKEN",
    "BIFF_RELAY_URL",
    "BIFF_RELAY_USER_CREDENTIALS",
)


class UnsupportedOverrideError(ValueError):
    """The client's environment overrides something biffd cannot yet carry."""


@final
class ClientEnvironment:
    """Reads the settings a client's environment sets for its session.

    biffd resolves a session in its own process, so it cannot see the client's
    environment.  The user override travels in the hello; relay overrides are
    refused by name until they can, instead of being dropped without a word.
    """

    @staticmethod
    def user(explicit: str, environ: Mapping[str, str]) -> str:
        """Return *explicit*, else the client's ``BIFF_USER``, else empty."""
        return explicit or environ.get("BIFF_USER", "")

    @staticmethod
    def require_no_relay_overrides(environ: Mapping[str, str]) -> None:
        """Refuse a client whose environment overrides the relay.

        Raises:
            UnsupportedOverrideError: A ``BIFF_RELAY_*`` variable is set.
        """
        set_here = [name for name in _RELAY_OVERRIDES if name in environ]
        if set_here:
            msg = (
                f"{', '.join(set_here)} cannot be sent to biffd yet and would be "
                "ignored; unset it, or run biff without the daemon"
            )
            raise UnsupportedOverrideError(msg)
