"""``biff doctor``'s end-to-end channel check: ask each session, report each answer.

For this user's running sessions, doctor has biffd send a channel check and wait for
the model's ``channel_ack`` (``biff.daemon.channel_check_methods``).  Every line is
informational: polling still delivers messages, so an unconfirmed channel never fails
doctor.  Relay-supplied names are stripped of control characters before printing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self, final

from nats.errors import Error as NatsError

from biff.formatting import sanitized_address, terminal_safe
from biff.plugins.claude_code.channel_check_ask import CheckNotSentError, DaemonAsk
from biff.plugins.contract import CheckResult
from biff.relay import PRESENCE_LIVENESS_SECONDS

if TYPE_CHECKING:
    from collections.abc import Callable

    from biff.models import UserSession
    from biff.plugins.claude_code.channel_check_ask import ChannelAsk
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

    __slots__ = ("_asker", "_now", "_target", "_timeout")

    _asker: ChannelAsk
    _target: CheckTarget
    _timeout: float
    _now: Callable[[], datetime]

    def __new__(
        cls,
        target: CheckTarget,
        *,
        ask: Callable[[], ChannelAsk] = lambda: DaemonAsk.for_this_process(None),
        timeout: float = WAIT_SECONDS,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> Self:
        """Check *target*, waiting up to *timeout* seconds; *ask* makes biffd's ask."""
        self = super().__new__(cls)
        self._asker = ask()
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
            lines = await asyncio.gather(*(self._ask(s) for s in sessions))
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
                shown = f"cannot check {terminal_safe(user)!r}"
                notes.append(self._note(f"{shown}: not a valid biff handle"))
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

    async def _ask(self, session: UserSession) -> CheckResult:
        name = sanitized_address(session.user, session.tty_name or session.tty)
        key = f"{session.user}:{session.tty}"
        try:
            is_live = await self._asker.confirmed(key, self._timeout)
        except CheckNotSentError as exc:
            return self._line(name, f"channel not confirmed (not sent: {exc})")
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
