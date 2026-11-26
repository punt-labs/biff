"""The daemon's own environment must not decide any session's identity or relay."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, final

if TYPE_CHECKING:
    from collections.abc import MutableMapping

__all__ = ["DaemonEnvironment"]

_SESSION_SETTINGS: Final = (
    "BIFF_OWNER",
    "BIFF_RELAY_NKEYS_SEED",
    "BIFF_RELAY_TLS_HANDSHAKE_FIRST",
    "BIFF_RELAY_TOKEN",
    "BIFF_RELAY_URL",
    "BIFF_RELAY_USER_CREDENTIALS",
    "BIFF_USER",
)


@final
class DaemonEnvironment:
    """Removes the per-session settings the daemon inherited from its launcher."""

    @staticmethod
    def scrub(environ: MutableMapping[str, str]) -> tuple[str, ...]:
        """Delete every per-session ``BIFF_*`` setting from *environ*.

        The daemon serves many users' sessions; a variable set where it was
        started would otherwise override each session's own configuration.
        Returns the names removed, so the caller can say so.
        """
        removed = tuple(name for name in _SESSION_SETTINGS if name in environ)
        for name in removed:
            del environ[name]
        return removed
