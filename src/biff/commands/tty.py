"""``biff tty`` — name the current CLI session."""

from __future__ import annotations

from biff.cli_context import CliContext
from biff.commands._result import CommandResult
from biff.commands._session import update_current_session
from biff.tty import rename_tty, validate_tty_name


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

    # A CLI session stores its own name; keep that record in step with the
    # relay or the next attach restores the old name.  If the store refuses,
    # undo the relay rename so the two never disagree.
    if ctx.rename_record is not None and claimed != ctx.tty_name:
        try:
            await ctx.rename_record(claimed)
        except BaseException:
            await _undo_rename(ctx, claimed)
            raise

    # Update the frozen CliContext so subsequent calls (REPL) and
    # cli_session cleanup see the CURRENT name, not the stale original.
    object.__setattr__(ctx, "tty_name", claimed)
    # Keep the talk state's outgoing from_tty in sync with the new name.
    ctx.talk.set_tty_name(claimed)

    await update_current_session(ctx, tty_name=claimed)
    # Keep the resume-reclaim hint current so the next resume reclaims the
    # RENAMED alias, not the stale one. The routing token is ctx.tty (the
    # session_id under identity routing).
    await ctx.relay.set_session_tty_hint(ctx.user, ctx.tty, claimed)
    return CommandResult(
        text=f"TTY: {claimed}",
        json_data={"tty": claimed},
    )


async def _undo_rename(ctx: CliContext, claimed: str) -> None:
    """Return the relay to ``ctx.tty_name`` after a failed record rename."""
    old = ctx.tty_name
    if old:
        await ctx.relay.reserve_tty_name(ctx.user, old, ctx.session_key)
    await ctx.relay.release_tty_name(ctx.user, claimed)
    session = await ctx.relay.get_session(ctx.session_key)
    if session is not None:
        await ctx.relay.update_session(session.model_copy(update={"tty_name": old}))
