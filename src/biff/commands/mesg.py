"""``biff mesg`` — control message reception."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.models import UserSession
from biff.tty import get_hostname, get_pwd

_VALID_VALUES = frozenset(("on", "off", "y", "n"))


async def mesg(ctx: CliContext, value: str) -> CommandResult:
    """Control message reception (on/off).

    *value* must be one of ``on``, ``off``, ``y``, ``n``.
    """
    value = value.strip().lower()
    if value not in _VALID_VALUES:
        msg = "Usage: biff mesg <on|off|y|n>"
        return CommandResult(text=msg, json_data={"error": msg}, error=True)

    enabled = value in ("on", "y")
    session = await ctx.relay.get_session(ctx.session_key)
    if session is None:
        session = UserSession(
            user=ctx.user,
            tty=ctx.tty,
            tty_name="cli",
            hostname=get_hostname(),
            pwd=get_pwd(),
        )
    updated = session.model_copy(update={"biff_enabled": enabled})
    await ctx.relay.update_session(updated)
    label = "y" if enabled else "n"
    return CommandResult(
        text=f"is {label}",
        json_data={"mesg": label},
    )
