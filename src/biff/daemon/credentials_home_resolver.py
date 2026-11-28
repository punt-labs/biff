"""The one resolution step that keeps biffd's seeded credentials at the seeded relay."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from biff.daemon.relays import ResolvedSession
    from biff.daemon.resolver import SessionResolver
    from biff.frame_client import Hello
    from biff.relay_seed import RelaySeed

__all__ = ["CredentialsHomeResolver"]


@final
class CredentialsHomeResolver:
    """Resolves through another resolver, keeping biffd's seeded credentials home.

    Every path that opens a relay for a session resolves it here: a hello, a
    restore at start, the sweep and the spill replay.  A session that would send
    a seeded credential to a relay other than the seeded one is refused, so no path
    can hand a repository's own relay the credentials biffd was started with.
    """

    __slots__ = ("_inner", "_seed")

    _inner: SessionResolver
    _seed: RelaySeed

    def __new__(cls, inner: SessionResolver, seed: RelaySeed) -> Self:
        """Resolve through *inner*, checking each session against *seed*."""
        self = super().__new__(cls)
        self._inner = inner
        self._seed = seed
        return self

    async def resolve(self, hello: Hello) -> ResolvedSession:
        """Return the configuration for *hello*.

        Raises:
            ResolveError: The configuration cannot be resolved.
            RelayConflictError: The session would send a seeded credential to
                another relay.
        """
        resolved = await self._inner.resolve(hello)
        self._seed.require_credentials_home(
            resolved.config.relay_url, resolved.config.relay_auth
        )
        return resolved
