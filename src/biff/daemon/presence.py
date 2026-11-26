"""A session's presence row in the relay: created once, recreated when it vanishes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from biff.models import SessionEvent, UserSession
from biff.relay_errors import RELAY_ERRORS
from biff.tty import claim_tty_name, get_hostname

if TYPE_CHECKING:
    from biff.models import BiffConfig
    from biff.relay import Relay
    from biff.session_routing import SessionRouting

__all__ = ["PresenceRow"]

logger = logging.getLogger(__name__)


@final
class PresenceRow:
    """Keeps one session listed in the relay.

    The row, and the tty name it holds, outlive every connection.  They can
    still vanish while no one is attached (the relay's own expiry, an operator's
    cleanup), so a session that becomes attached again calls :meth:`ensure`.
    """

    __slots__ = ("_config", "_cwd", "_relay", "_routing")

    _relay: Relay
    _config: BiffConfig
    _routing: SessionRouting
    _cwd: str

    def __new__(
        cls, relay: Relay, config: BiffConfig, routing: SessionRouting, cwd: str
    ) -> Self:
        """Create the presence of *routing*'s session, working in *cwd*."""
        self = super().__new__(cls)
        self._relay = relay
        self._config = config
        self._routing = routing
        self._cwd = cwd
        return self

    @property
    def key(self) -> str:
        """The session's relay key."""
        return self._routing.session_key(self._config.user)

    async def ensure(self) -> UserSession:
        """Return the session's row, creating it if the relay has none.

        An existing row is kept and its liveness refreshed; a missing one is
        created with a newly claimed tty name.
        """
        row = await self._relay.get_session(self.key)
        if row is not None:
            await self._relay.heartbeat(self.key)
            return row
        config = self._config
        row = UserSession(
            user=config.user,
            tty=self._routing.routing_id,
            tty_name=await claim_tty_name(self._relay, config.user, self.key),
            hostname=get_hostname(),
            pwd=self._cwd,
            display_name=config.display_name,
            kind=config.kind,
            launched_by=config.launched_by,
            owner_git=config.owner_git,
            repo=config.repo_name,
        )
        await self._relay.update_session(row)
        await self._login(row)
        return row

    async def _login(self, row: UserSession) -> None:
        """Record the session's login for ``last``; history must not fail a hello."""
        try:
            await self._relay.append_wtmp(
                SessionEvent(
                    session_key=self.key,
                    event="login",
                    user=row.user,
                    tty=row.tty,
                    tty_name=row.tty_name,
                    hostname=row.hostname,
                    pwd=row.pwd,
                    repo=row.repo,
                )
            )
        except RELAY_ERRORS:
            logger.warning("could not record a login for %s", self.key, exc_info=True)
