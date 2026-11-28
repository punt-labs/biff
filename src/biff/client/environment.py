"""What a client carries from its own environment into its hello."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from biff.relay_seed import RELAY_VARIABLES, RelaySeed

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["ClientEnvironment"]


@final
class ClientEnvironment:
    """Reads the settings a client's environment sets for its session.

    biffd resolves a session in its own process, so it cannot see the client's
    environment.  The user override travels in the hello; the relay settings
    travel as digests, which biffd compares with the ones it was started with.
    """

    @staticmethod
    def user(explicit: str, environ: Mapping[str, str]) -> str:
        """Return *explicit*, else the client's ``BIFF_USER``, else empty."""
        return explicit or environ.get("BIFF_USER", "")

    @staticmethod
    def relay_seed(environ: Mapping[str, str]) -> RelaySeed:
        """Return the digests of the ``BIFF_RELAY_*`` values *environ* sets."""
        return RelaySeed.of(environ)

    @staticmethod
    def carries_relay_settings(environ: Mapping[str, str]) -> bool:
        """Return whether *environ* sets a ``BIFF_RELAY_*`` override.

        A process that does is a runner handed its relay and credentials
        explicitly, such as a CI job posting to the wall.
        """
        return any(name in environ for name in RELAY_VARIABLES)
