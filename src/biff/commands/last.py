"""``biff last`` — show session login/logout history."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.formatting import format_last, pair_events
from biff.tty import build_session_key


async def last(ctx: CliContext, user: str, count: int) -> CommandResult:
    """Show session login/logout history."""
    count = max(1, min(count, 100))
    filter_user: str | None = None
    if user:
        filter_user = user.strip().lstrip("@")

    events = await ctx.relay.get_wtmp(user=filter_user, count=count * 2)
    if not events:
        return CommandResult(text="No session history.", json_data=[])

    current_sessions = await ctx.relay.get_sessions_for_repos(ctx.config.visible_repos)
    active_keys = {build_session_key(s.user, s.tty) for s in current_sessions}
    pairs = pair_events(events)
    pairs = pairs[:count]

    result: list[dict[str, object]] = []
    for login, logout in pairs:
        entry: dict[str, object] = {
            "user": login.user,
            "tty": login.tty_name or login.tty[:8] or "-",
            "login": login.timestamp.isoformat(),
            "logout": logout.timestamp.isoformat() if logout else None,
            "active": login.session_key in active_keys,
        }
        result.append(entry)

    return CommandResult(
        text=format_last(pairs, active_keys),
        json_data=result,
    )
