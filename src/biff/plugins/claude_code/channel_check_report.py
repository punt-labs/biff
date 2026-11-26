"""``biff doctor``'s end-to-end channel check: ask each session, report each answer.

For this user's running sessions in the repo, doctor sends a channel check over
the relay and waits for the model to acknowledge it with ``channel_ack`` (the
server side is ``biff.server.channel_check``).  Every line is informational:
polling still delivers messages, so a channel that is not confirmed never fails
doctor.  Names that come from the relay are stripped of control characters
before they are printed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self, final

from nats.errors import Error as NatsError

from biff._formatting import terminal_safe
from biff.formatting import sanitized_address
from biff.plugins.contract import CheckResult
from biff.relay import PRESENCE_LIVENESS_SECONDS

if TYPE_CHECKING:
    from collections.abc import Callable

    from biff.models import UserSession
    from biff.plugins.claude_code.channel_check_relay import CheckRelay
    from biff.plugins.claude_code.channel_check_target import CheckTarget

__all__ = ["ChannelCheckReport"]

WAIT_SECONDS = 30.0
"""How long doctor waits for the sessions to acknowledge."""

_NAME = "Channel"
_NEEDS_NATS = "the end-to-end channel check needs the NATS relay"
_NOT_CONFIRMED = (
    "channel not confirmed. The Channels lines above name what can block it; "
    "also check that Claude Code was started with biff's channel and that "
    "biff's server is connected"
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
        """Return one line per live session, or the lines saying why there is none."""
        relay = self._target.relay
        if relay is None:
            return (self._note(_NEEDS_NATS),)
        try:
            sessions, notes = await self._live_sessions(relay)
            if not sessions and not notes:
                return (
                    self._note("nothing to check: no running biff session", ok=True),
                )
            lines = await asyncio.gather(*(self._ask(relay, s) for s in sessions))
        except ConnectionError:
            return (self._note(f"{_NEEDS_NATS} (the relay is unreachable)"),)
        finally:
            await relay.close()
        return (*notes, *lines)

    async def _live_sessions(
        self, relay: CheckRelay
    ) -> tuple[list[UserSession], list[CheckResult]]:
        """Look up each handle; a handle that cannot be looked up is a note."""
        now = self._now()
        live: list[UserSession] = []
        notes: list[CheckResult] = []
        for user in self._target.users:
            try:
                sessions = await relay.list_sessions_for_user(user)
            except ValueError:
                shown = terminal_safe(user)
                notes.append(
                    self._note(f"cannot check {shown!r}: not a valid biff handle")
                )
            except ConnectionError:
                raise  # the relay is unreachable: results() says so once
            except (OSError, TimeoutError, NatsError):
                notes.append(
                    self._note(
                        f"could not list the running biff sessions of "
                        f"{terminal_safe(user)!r} (the presence lookup failed)"
                    )
                )
            else:
                live += [
                    s
                    for s in sessions
                    if s.is_live(now=now, ttl_seconds=PRESENCE_LIVENESS_SECONDS)
                ]
        return live, notes

    async def _ask(self, relay: CheckRelay, session: UserSession) -> CheckResult:
        name = sanitized_address(session.user, session.tty_name or session.tty)
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
        text = f"{name}: {message}"
        return CheckResult(_NAME, passed=ok, message=text, required=False)

    @staticmethod
    def _note(message: str, *, ok: bool = False) -> CheckResult:
        return CheckResult(_NAME, passed=ok, message=message, required=False)
