"""``biff tty`` — name the current CLI session."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.models import UserSession
from biff.tty import get_hostname, get_pwd, rename_tty, validate_tty_name


async def tty(ctx: CliContext, name: str) -> CommandResult:
    """Name the current CLI session."""
    name = name.strip()

    if name:
        error = validate_tty_name(name)
        if error:
            return CommandResult(text=error, json_data={"error": error}, error=True)

    # Claim new name, then release old on success (DES-035).
    try:
        claimed = await rename_tty(
            ctx.relay,
            ctx.user,
            ctx.session_key,
            ctx.tty_name,
            preferred=name or None,
        )
    except ValueError:
        msg = f"Error: name {name!r} already in use by another session."
        return CommandResult(text=msg, json_data={"error": msg}, error=True)
    except RuntimeError:
        msg = "Error: failed to claim TTY name after retries."
        return CommandResult(text=msg, json_data={"error": msg}, error=True)

    # Update the frozen CliContext so subsequent calls (REPL) and
    # cli_session cleanup see the CURRENT name, not the stale original.
    object.__setattr__(ctx, "tty_name", claimed)
    # Keep the talk state's outgoing from_tty in sync with the new name.
    ctx.talk.set_tty_name(claimed)

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
