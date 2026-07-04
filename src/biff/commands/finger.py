"""``biff finger`` — check what a user is working on."""

from __future__ import annotations

from biff.cli_session import CliContext
from biff.commands._result import CommandResult
from biff.formatting import format_finger, format_finger_multi
from biff.relay import live_sessions
from biff.server.tools._session import resolve_tty_name
from biff.tty import parse_address


async def finger(ctx: CliContext, user: str) -> CommandResult:
    """Check what one or more users are working on.

    Accepts space-separated addresses: ``@user1 @user2 @user3``.
    """
    addresses = user.split()
    # Availability check: only consider sessions that are actually live
    # (heartbeat within the liveness window), not dead ones lingering in
    # the KV until the storage TTL (biff-mue).
    all_sessions = live_sessions(
        await ctx.relay.get_sessions_for_repos(ctx.visible_repos)
    )

    blocks: list[str] = []
    json_parts: list[object] = []
    has_error = False

    for addr in addresses:
        try:
            bare_user, tty = parse_address(addr)
        except ValueError as exc:
            blocks.append(str(exc))
            json_parts.append({"error": str(exc)})
            has_error = True
            continue

        if tty:
            session = resolve_tty_name(
                all_sessions, bare_user, tty, local_repo=ctx.config.repo_name
            )
            if session is None:
                blocks.append(f"Login: {bare_user}\nNo session on tty {tty}.")
                json_parts.append({"error": f"No session on tty {tty}."})
                has_error = True
            else:
                blocks.append(format_finger(session))
                json_parts.append(session.model_dump(mode="json"))
        else:
            sessions = [s for s in all_sessions if s.user == bare_user]
            if not sessions:
                blocks.append(f"Login: {bare_user}\nNever logged in.")
                json_parts.append({"error": f"{bare_user} never logged in."})
                has_error = True
            else:
                blocks.append(format_finger_multi(sessions))
                json_parts.append([s.model_dump(mode="json") for s in sessions])

    return CommandResult(
        text="\n\n".join(blocks),
        json_data=json_parts[0] if len(json_parts) == 1 else json_parts,
        error=has_error,
    )
