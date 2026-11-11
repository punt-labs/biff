"""Durable session registration: relay row, TTY alias, and the active marker."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from biff._stdlib import active_dir
from biff.models import UserSession
from biff.relay import Relay
from biff.tty import build_session_key, claim_tty_name, validate_reclaimable_name

logger = logging.getLogger(__name__)


def write_active_session(
    repo_name: str, session_key: str, worktree_root: str = ""
) -> None:
    """Mark a session as active so SessionEnd hooks can find it.

    Writes ``~/.punt-labs/biff/active/{safe_key}`` containing the session key,
    repo name, and worktree root (one per line).  The SessionEnd hook
    reads these files and converts them to sentinels for the reaper
    to process.  The third line is optional for backwards compatibility.
    """
    d = active_dir()
    d.mkdir(parents=True, exist_ok=True)
    safe = session_key.replace(":", "-")
    (d / safe).write_text(f"{session_key}\n{repo_name}\n{worktree_root}\n")


def write_marker(repo_name: str, session_key: str, worktree_root: str) -> None:
    """Write the active-session marker, logging OSError rather than swallowing it.

    A missing marker breaks SessionEnd hook cleanup — failures must be
    visible in logs so operators can diagnose them.
    """
    try:
        write_active_session(repo_name, session_key, worktree_root=worktree_root)
    except OSError as exc:
        logger.warning("Failed to write active marker for %s: %s", session_key, exc)


async def register_session(
    relay: Relay,
    user: str,
    tty_hex: str,
    *,
    display_name: str,
    kind: str,
    hostname: str,
    pwd: str,
    repo: str,
    preferred_name: str | None = None,
    on_claim: Callable[[str], None] = lambda _name: None,
) -> tuple[UserSession, str]:
    """Claim a TTY name, then write the session row once with tty_name set.

    Single atomic registration: if the TTY claim fails, no KV row is
    written; if the claim succeeds, the row is written exactly once with
    ``tty_name`` already populated.  Replaces the previous two-write
    pattern (write-then-claim-then-write) that left half-initialised
    rows in KV when anything failed between the two writes.

    Returns ``(session, tty_name)``.  Does not write an active-marker
    file; the caller owns that.
    """
    session_key = build_session_key(user, tty_hex)
    # Resume reclaim: when no name is explicitly requested, prefer
    # the ttyN this session_id last held so a resumed session keeps its alias.
    # The routing token (tty_hex) is the session_id under identity routing.
    from_hint = False
    if preferred_name is None:
        # The reclaim hint value is team-writable and flows into a KV key /
        # NATS subject via claim_tty_name — validate at this trust boundary.
        # A malformed or namespace-colliding value is ignored (fall back to a
        # fresh alias), never fed to claim_tty_name.
        candidate = await relay.get_session_tty_hint(user, tty_hex)
        if candidate is not None and validate_reclaimable_name(candidate) is None:
            preferred_name = candidate
            from_hint = True
        elif candidate is not None:
            logger.warning(
                "ignoring malformed reclaim hint %r for %s; using a fresh alias",
                candidate,
                user,
            )
    try:
        tty_name = await claim_tty_name(
            relay, user, session_key, preferred=preferred_name
        )
        if from_hint:
            logger.info("reclaimed prior alias %s on resume", tty_name)
    except ValueError:
        # The fallback fires ONLY for a name that came from the resume hint
        # (from_hint) — the prior ttyN was taken while this session was gone
        # (edge case 6), so reassign the lowest-free alias.  Routing is
        # unaffected: the inbox keys on the session_id, not the display alias;
        # only the human-facing alias moves.  An explicitly-requested name
        # (from_hint is False) still raises to its caller.
        if not from_hint:
            raise
        tty_name = await claim_tty_name(relay, user, session_key, preferred=None)
        logger.info(
            "prior alias %s taken on resume; reassigned %s",
            preferred_name,
            tty_name,
        )
    on_claim(tty_name)
    # Release any stale TTY name reservation left by a prior process that
    # crashed after writing the KV row but before releasing its name.  The
    # KV row itself is overwritten unconditionally below; without this
    # check the old reservation leaks and compounds across crash-restart
    # cycles.  Self-release is skipped when the stale name matches the
    # newly claimed one.
    #
    # Ownership check (PR #215 round 3): only release when the reservation
    # is still owned by *our* session_key.  If another live session has
    # reclaimed the name since the crashed owner wrote the stale row, we
    # must not revoke their reservation.  Use the authoritative *user*
    # parameter (not ``existing.user``) so a corrupted/out-of-sync payload
    # cannot direct a release against a different user's namespace.
    existing = await relay.get_session(session_key)
    if existing is not None and existing.tty_name and existing.tty_name != tty_name:
        owner = await relay.get_tty_reservation_owner(user, existing.tty_name)
        if owner == session_key:
            try:
                await relay.release_tty_name(user, existing.tty_name)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.warning(
                    "Failed to release stale TTY name %s for %s",
                    existing.tty_name,
                    session_key,
                )
    # Both activity timestamps start identical: a session that has never
    # invoked a tool reads idle as time-since-registration, its own
    # meaningful start time — not epoch-era or spuriously fresh.
    registered_at = datetime.now(UTC)
    session = UserSession(
        user=user,
        tty=tty_hex,
        tty_name=tty_name,
        display_name=display_name,
        kind=kind,
        hostname=hostname,
        pwd=pwd,
        repo=repo,
        last_active=registered_at,
        last_tool_at=registered_at,
    )
    await relay.update_session(session)
    # Record the claimed alias so the next resume of this session_id reclaims
    # it (survives clean-exit reservation release).
    await relay.set_session_tty_hint(user, tty_hex, tty_name)
    return session, tty_name
