"""The daemon's own environment must not decide any session's identity."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, final

if TYPE_CHECKING:
    from collections.abc import MutableMapping

__all__ = ["DaemonEnvironment"]

_SESSION_SETTINGS: Final = (
    "BIFF_OWNER",
    "BIFF_USER",
)


@final
class DaemonEnvironment:
    """Removes the per-session identity the daemon inherited from its launcher.

    The ``BIFF_RELAY_*`` variables stay: they are the relay settings biffd is
    started with (see ``biff.relay_seed``), and every client is compared with them.
    """

    @staticmethod
    def scrub(environ: MutableMapping[str, str]) -> tuple[str, ...]:
        """Delete every per-session identity setting from *environ*.

        The daemon serves many users' sessions; a user or owner set where it was
        started would otherwise override each session's own configuration.
        Returns the names removed, so the caller can say so.
        """
        removed = tuple(name for name in _SESSION_SETTINGS if name in environ)
        for name in removed:
            del environ[name]
        return removed
