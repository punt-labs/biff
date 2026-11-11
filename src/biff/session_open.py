"""Open a session: the durable record a SessionStart hook registers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from biff.models import BiffConfig, UnreadSummary
from biff.relay import Relay
from biff.session_handle import SessionHandleStore
from biff.session_registry import register_session, write_marker
from biff.session_routing import SessionRouting
from biff.unread_file import write_unread_file


@dataclass(frozen=True, slots=True)
class SessionOpener:
    """Registers a session's durable state from what a hook knows.

    The opener writes the relay row, the active marker, the unread status
    file and the handle, all keyed by :class:`SessionRouting`.  Live-client
    resources (heartbeat, subscriptions) are not its job: a hook exits in
    milliseconds, so the server process binds those on its first identified
    call.  ``open`` may run again for a resumed session: the relay row is
    overwritten in place and the handle rewritten, never duplicated.
    """

    relay: Relay
    config: BiffConfig
    repo_common_root: Path
    unread_dir: Path
    handles: SessionHandleStore
    hostname: str
    pwd: str

    async def open(self, routing: SessionRouting) -> str:
        """Register *routing*'s session and return its session key."""
        key = routing.session_key(self.config.user)
        _, tty_name = await register_session(
            self.relay,
            self.config.user,
            routing.routing_id,
            display_name=self.config.display_name,
            kind=self.config.kind,
            hostname=self.hostname,
            pwd=self.pwd,
            repo=self.config.repo_name,
        )
        write_marker(self.config.repo_name, key, str(self.repo_common_root))
        write_unread_file(
            routing.unread_path(self.unread_dir),
            UnreadSummary(count=0),
            repo_name=self.config.repo_name,
            user=self.config.user,
            tty_name=tty_name,
            biff_enabled=True,
        )
        self.handles.write(routing, key)
        return key


async def open_session_from_payload(
    data: dict[str, object],
    *,
    user_override: str | None = None,
    relay_url_override: str | None = None,
) -> str | None:
    """Open the session a SessionStart hook payload names.

    Returns the session key, or ``None`` when the payload carries no usable
    ``session_id`` (a missing or malformed one is the harness failing to
    declare itself, and a hook must never break session start over it).
    The relay is the one the server will use, selected by the same config.
    """
    from biff._stdlib import biff_data_dir  # noqa: PLC0415
    from biff.config import RELAY_URL_UNSET, load_mcp_config  # noqa: PLC0415
    from biff.server.state import create_state  # noqa: PLC0415
    from biff.session_routing import SessionIdError  # noqa: PLC0415

    raw = data.get("session_id")
    try:
        routing = SessionRouting(raw if isinstance(raw, str) else "")
    except SessionIdError:
        return None
    cwd = data.get("cwd")
    resolved = load_mcp_config(
        user_override=user_override,
        relay_url_override=(
            RELAY_URL_UNSET if relay_url_override is None else relay_url_override
        ),
        start=Path(cwd) if isinstance(cwd, str) and cwd else None,
    )
    state = create_state(resolved.config, resolved.data_dir, tty=routing.routing_id)
    data_root = biff_data_dir()
    try:
        return await SessionOpener(
            relay=state.relay,
            config=resolved.config,
            repo_common_root=resolved.repo_common_root,
            unread_dir=data_root / "unread",
            handles=SessionHandleStore(data_root / "sessions"),
            hostname=state.hostname,
            pwd=state.pwd,
        ).open(routing)
    finally:
        await state.relay.close()
