"""``biff doctor``'s end-to-end channel check: ask each session, report each answer.

For this user's running sessions in the repo, doctor sends a channel check over
the relay and waits for the model to acknowledge it with ``channel_ack`` (the
server side is ``biff.server.channel_check``).  Every line is informational:
polling still delivers messages, so a channel that is not confirmed never fails
doctor.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, Self, final

from nats.errors import Error as NatsError

from biff.config import load_cli_config
from biff.nats_relay import NatsRelay
from biff.plugins.contract import CheckResult
from biff.relay import PRESENCE_LIVENESS_SECONDS
from biff.relay_select import RelaySelector

if TYPE_CHECKING:
    from collections.abc import Callable

    from biff.models import UserSession

__all__ = ["ChannelCheckReport", "CheckTarget"]

WAIT_SECONDS = 30.0
"""How long doctor waits for the sessions to acknowledge."""

_NAME = "Channel"
_NEEDS_NATS = "the end-to-end channel check needs the NATS relay"
_NOT_CONFIRMED = (
    "channel not confirmed. The Channels lines above name what can block it; "
    "also check that Claude Code was started with biff's channel and that "
    "biff's server is connected"
)


class CheckPending(Protocol):
    """A check in flight: wait for its answer, then close it."""

    async def confirmed(self, timeout: float) -> bool:
        """Whether the session answered within *timeout* seconds."""
        ...

    async def close(self) -> None:
        """Stop listening for the answer."""
        ...


class CheckRelay(Protocol):
    """What the check uses of a relay; ``NatsRelay`` is the one that has it."""

    async def get_sessions_for_user(self, user: str) -> list[UserSession]:
        """Return the sessions *user* has in this relay's repo."""
        ...

    async def request_channel_check(self, session_key: str) -> CheckPending:
        """Send a check to *session_key* and return the wait for its answer."""
        ...

    async def close(self) -> None:
        """Close the connection."""
        ...


@dataclass(frozen=True, slots=True)
class CheckTarget:
    """Whose sessions to check and where; no relay means the check cannot run."""

    relay: CheckRelay | None  # absent when the relay is not NATS
    user: str

    @classmethod
    def from_environment(cls) -> Self:
        """Resolve the configured user, repo and relay the way the CLI does.

        Without a repository or an identity, or with the local relay, there is
        no relay to ask.
        """
        try:
            resolved = load_cli_config()
        except SystemExit:
            return cls(None, "")
        relay = RelaySelector(resolved.config, resolved.data_dir).select(dormant=False)
        return cls(
            relay if isinstance(relay, NatsRelay) else None, resolved.config.user
        )


@final
class ChannelCheckReport:
    """Run the check against a target and turn the answers into doctor lines."""

    __slots__ = ("_now", "_target", "_timeout")

    _target: CheckTarget
    _timeout: float
    _now: Callable[[], datetime]

    def __new__(
        cls,
        target: CheckTarget,
        *,
        timeout: float = WAIT_SECONDS,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> Self:
        """Check *target*, waiting up to *timeout* seconds for the answers."""
        self = super().__new__(cls)
        self._target = target
        self._timeout = timeout
        self._now = now
        return self

    async def results(self) -> tuple[CheckResult, ...]:
        """Return one line per live session, or one line saying why there is none."""
        relay = self._target.relay
        if relay is None:
            return (self._note(_NEEDS_NATS),)
        try:
            sessions = await self._live_sessions(relay)
            if not sessions:
                return (
                    self._note("nothing to check: no running biff session", ok=True),
                )
            lines = await asyncio.gather(*(self._ask(relay, s) for s in sessions))
        except (OSError, TimeoutError, NatsError):
            return (self._note(f"{_NEEDS_NATS} (the relay is unreachable)"),)
        finally:
            await relay.close()
        return tuple(lines)

    async def _live_sessions(self, relay: CheckRelay) -> list[UserSession]:
        now = self._now()
        sessions = await relay.get_sessions_for_user(self._target.user)
        return [
            s
            for s in sessions
            if s.is_live(now=now, ttl_seconds=PRESENCE_LIVENESS_SECONDS)
        ]

    async def _ask(self, relay: CheckRelay, session: UserSession) -> CheckResult:
        name = f"{session.user}:{session.tty_name or session.tty}"
        try:
            pending = await relay.request_channel_check(f"{session.user}:{session.tty}")
        except (OSError, TimeoutError, NatsError):
            return self._line(name, "channel not confirmed (the check was not sent)")
        try:
            is_live = await pending.confirmed(self._timeout)
        finally:
            await pending.close()
        if is_live:
            return self._line(name, "channel live", ok=True)
        return self._line(name, _NOT_CONFIRMED)

    @staticmethod
    def _line(name: str, message: str, *, ok: bool = False) -> CheckResult:
        return CheckResult(
            _NAME, passed=ok, message=f"{name}: {message}", required=False
        )

    @staticmethod
    def _note(message: str, *, ok: bool = False) -> CheckResult:
        return CheckResult(_NAME, passed=ok, message=message, required=False)
