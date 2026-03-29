"""``biff tty`` — name the current CLI session."""

from __future__ import annotations

import logging

from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.models import UserSession
from biff.tty import claim_tty_name, get_hostname, get_pwd, validate_tty_name

logger = logging.getLogger(__name__)


async def tty(ctx: CliContext, name: str) -> CommandResult:
    """Name the current CLI session."""
    name = name.strip()

    if name:
        error = validate_tty_name(name)
        if error:
            return CommandResult(text=error, json_data={"error": error}, error=True)

    # Release old reservation before claiming new one (DES-035).
    if ctx.tty_name:
        try:
            await ctx.relay.release_tty_name(ctx.user, ctx.tty_name)
        except Exception:  # noqa: BLE001
            logger.debug("Failed to release old TTY name %s", ctx.tty_name)

    try:
        if name:
            claimed = await claim_tty_name(
                ctx.relay, ctx.user, ctx.session_key, preferred=name
            )
        else:
            claimed = await claim_tty_name(ctx.relay, ctx.user, ctx.session_key)
    except ValueError:
        # Re-reserve old name on failure.
        if ctx.tty_name:
            try:
                await ctx.relay.reserve_tty_name(
                    ctx.user, ctx.tty_name, ctx.session_key
                )
            except Exception:  # noqa: BLE001
                logger.debug("Failed to re-reserve old TTY name %s", ctx.tty_name)
        msg = f"Error: name {name!r} already in use by another session."
        return CommandResult(text=msg, json_data={"error": msg}, error=True)

    session = await ctx.relay.get_session(ctx.session_key)
    if session is None:
        session = UserSession(
            user=ctx.user,
            tty=ctx.tty,
            tty_name=claimed,
            hostname=get_hostname(),
            pwd=get_pwd(),
        )
    else:
        session = session.model_copy(update={"tty_name": claimed})
    await ctx.relay.update_session(session)
    return CommandResult(
        text=f"TTY: {claimed}",
        json_data={"tty": claimed},
    )
