"""``biff mesg`` — control message reception."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.commands._session import update_current_session

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
    await update_current_session(ctx, biff_enabled=enabled)
    label = "y" if enabled else "n"
    return CommandResult(
        text=f"is {label}",
        json_data={"mesg": label},
    )
