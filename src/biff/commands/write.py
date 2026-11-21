"""``biff write`` — send a message to a teammate."""

from __future__ import annotations

from biff.chunking import chunk_message
from biff.cli_context import CliContext
from biff.commands._messaging import deliver_with_retry
from biff.commands._result import CommandResult
from biff.server.tools._write_target import WriteRouter
from biff.tty import parse_address


async def write(ctx: CliContext, to: str, message: str) -> CommandResult:
    """Send a message to a teammate's inbox."""
    try:
        bare_user, tty = parse_address(to)
    except ValueError as exc:
        return CommandResult(
            text=str(exc),
            json_data={"status": "error", "to": to, "error": str(exc)},
            error=True,
        )
    try:
        target = await WriteRouter(
            local_repo=ctx.config.repo_name, visible_repos=ctx.visible_repos
        ).find(ctx.relay, bare_user, tty)
    except ValueError as exc:
        return CommandResult(
            text=str(exc),
            json_data={"status": "error", "to": to, "error": str(exc)},
            error=True,
        )

    chunks = chunk_message(message)
    try:
        delivery = await deliver_with_retry(
            ctx.relay,
            from_user=ctx.user,
            from_tty=ctx.tty_name,
            session_key=ctx.session_key,
            to_user=target.relay_key,
            target_repo=target.target_repo,
            chunks=chunks,
            display=target.display,
        )
    except ValueError as exc:
        # A deterministic validation failure (deliver_with_retry does not
        # retry these) — same reporting shape as the recipient-resolution
        # errors above.
        return CommandResult(
            text=str(exc),
            json_data={"status": "error", "to": to, "error": str(exc)},
            error=True,
        )
    if not delivery.ok:
        return CommandResult(
            text=delivery.text,
            json_data={"status": "error", "to": to, "error": delivery.text},
            error=True,
        )
    return CommandResult(
        text=target.report(delivery),
        json_data={
            "status": "forwarded" if target.forwarded else "sent",
            "to": target.display,
            "parts": len(chunks),
        },
    )
