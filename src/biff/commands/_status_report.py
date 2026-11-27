"""Build the ``biff status`` report: connection state, session and unread."""

from __future__ import annotations

from importlib.metadata import version as pkg_version
from typing import TYPE_CHECKING, Self, final

from biff.commands._relay_counters import RelayCounters
from biff.commands._relay_line import RelayLine
from biff.commands._result import CommandResult
from biff.formatting import format_idle, format_wall_status_line

if TYPE_CHECKING:
    from biff.cli_context import CliContext
    from biff.models import UserSession, WallPost

__all__ = ["StatusReport"]


@final
class StatusReport:
    """Connection state, session info, pending messages and relay counters."""

    __slots__ = ("_ctx",)

    _ctx: CliContext

    def __new__(cls, ctx: CliContext) -> Self:
        """Report the status of the session of *ctx*."""
        self = super().__new__(cls)
        self._ctx = ctx
        return self

    async def build(self) -> CommandResult:
        """Read the relay and return the report, as text and as JSON."""
        ctx = self._ctx
        session = await ctx.relay.get_session(ctx.session_key)
        unread = (await ctx.relay.get_unread_summary(ctx.session_key)).count
        wall = await ctx.relay.get_wall()
        relay = RelayLine.of(ctx.relay, ctx.config.relay_url)
        tty_name = session.tty_name if session else "cli"
        return CommandResult(
            text=self._text(relay, session, tty_name, unread, wall),
            json_data=self._data(relay, tty_name, unread, wall),
        )

    def _text(
        self,
        relay: RelayLine,
        session: UserSession | None,
        tty_name: str,
        unread: int,
        wall: WallPost | None,
    ) -> str:
        ctx = self._ctx
        idle = format_idle(session.last_active) if session else "?"
        wall_line = format_wall_status_line(wall) if wall else "wall: (none)"
        plural = "" if unread == 1 else "s"
        return "\n".join(
            [
                f"biff {pkg_version('punt-biff')}",
                relay.text,
                f"user: {ctx.user}",
                f"session: {tty_name} ({ctx.tty[:8]}), idle {idle}",
                f"unread: {unread} message{plural}",
                wall_line,
            ]
        )

    def _data(
        self, relay: RelayLine, tty_name: str, unread: int, wall: WallPost | None
    ) -> dict[str, object]:
        ctx = self._ctx
        return {
            "version": pkg_version("punt-biff"),
            "relay": relay.name,
            "relay_dir": relay.directory,
            "relay_counters": RelayCounters.json_of(ctx.relay),
            "user": ctx.user,
            "session_key": ctx.session_key,
            "tty_name": tty_name,
            "unread": unread,
            "wall": wall.model_dump(mode="json") if wall else None,
        }
