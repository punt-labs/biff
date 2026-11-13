"""Server state container for biff MCP tools.

All shared state lives in a frozen dataclass. Tool closures capture it
directly during registration; it is also available as the lifespan context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from biff.models import BiffConfig
from biff.nats_relay import NatsRelay
from biff.relay import DormantRelay, LocalRelay, Relay
from biff.server.activity import ActivityTracker
from biff.server.display_queue import DisplayQueue
from biff.session_routing import SessionRouting
from biff.talk_state import TalkState
from biff.tty import build_session_key, generate_tty, get_hostname, get_pwd


@dataclass(frozen=True)
class ServerState:
    """Immutable container for all server-wide shared state."""

    config: BiffConfig
    relay: Relay
    # Parent of ``git rev-parse --git-common-dir``: the same absolute path from
    # the main checkout and every linked worktree.  Used for cross-worktree
    # coordination (wall marker path, ``write_active_session`` third line so
    # ``_detect_collisions`` matches across linked worktrees) — the per-worktree
    # ``repo_root`` stays as-is for write-through to per-worktree files
    # (DES-054).  Required: ``create_state`` resolves
    # ``None`` inputs to ``repo_root`` (matching the loader's fallback), so the
    # field's runtime type is always ``Path`` and every reader is unconditional.
    repo_common_root: Path
    activity: ActivityTracker = field(default_factory=ActivityTracker)
    display_queue: DisplayQueue = field(default_factory=DisplayQueue)
    tty: str = ""
    hostname: str = ""
    pwd: str = ""
    unread_path: Path | None = None
    owns_relay: bool = True
    dormant: bool = False
    repo_root: Path | None = None
    org_repos: frozenset[str] = field(default_factory=lambda: frozenset[str]())
    # ``time.monotonic()`` of the last org-repos refresh attempt (success or
    # failure). ``None`` before the first attempt. Throttles ``_refresh_org_repos``
    # so this non-critical, stale-tolerant call stops competing with user-facing
    # calls for the shared connection's wedge-detection budget.
    org_repos_refreshed_at: float | None = None
    talk: TalkState = field(init=False)
    # The session this server is bound to, set once by ``bind`` on the first
    # identified call.  ``None`` until then.
    bound_session_id: str | None = None
    # Directory the unread status file lives in; ``bind`` resolves the
    # per-session file name inside it once the session is known.
    unread_dir: Path | None = None

    def __post_init__(self) -> None:
        """Compose the shared ephemeral talk state from this server's identity.

        ``TalkState`` is mutable and held by reference inside the frozen
        state — the same seating ``ActivityTracker`` and ``DisplayQueue``
        get.  The always-on talk subscription feeds it every frame and the
        state-returning tool drains it (talk_state.py, DES-020/021).  The
        display ``tty_name`` is populated after registration via
        ``TalkState.set_tty_name``.
        """
        object.__setattr__(
            self,
            "talk",
            TalkState(
                relay=self.relay,
                user=self.config.user,
                tty=self.tty,
                session_key=self.session_key,
            ),
        )

    @property
    def session_key(self) -> str:
        """Composite key ``{user}:{tty}`` for this server instance."""
        return build_session_key(self.config.user, self.tty)

    @property
    def is_bound(self) -> bool:
        """Whether a caller has declared the session this server serves."""
        return self.bound_session_id is not None

    def bind(self, routing: SessionRouting) -> None:
        """Point this server at the session *routing* names.

        The same session again is a no-op.  A different session_id on this
        long-lived process is the owning session changing id in place
        (``/clear``, resume), so the server re-points.  Releasing what the
        previous session registered is the caller's job: see
        ``_LiveSession.attach``, which tears it down before binding.
        """
        if self.bound_session_id == routing.session_id:
            return
        object.__setattr__(self, "bound_session_id", routing.session_id)
        object.__setattr__(self, "tty", routing.routing_id)
        if self.unread_dir is not None:
            object.__setattr__(
                self, "unread_path", routing.unread_path(self.unread_dir)
            )
        self.talk.rebind(
            tty=routing.routing_id, session_key=routing.session_key(self.config.user)
        )

    @property
    def visible_repos(self) -> frozenset[str]:
        """All repos visible to this session: self + explicit peers + org-discovered.

        Merges ``config.visible_repos`` (repo_name + explicit peers) with
        org-discovered repos from ``NatsRelay.discover_repos_for_org()``
        (DES-034).
        """
        if not self.org_repos:
            return self.config.visible_repos
        return self.config.visible_repos | self.org_repos


def create_state(
    config: BiffConfig,
    data_dir: Path,
    *,
    relay: Relay | None = None,
    unread_path: Path | None = None,
    unread_dir: Path | None = None,
    tty: str | None = None,
    hostname: str | None = None,
    pwd: str | None = None,
    dormant: bool = False,
    repo_root: Path | None = None,
    repo_common_root: Path | None = None,
    org_repos: frozenset[str] | None = None,
) -> ServerState:
    """Create a ``ServerState`` from config and data directory.

    Relay selection (in priority order):

    1. An explicit *relay* always wins (used by tests).
    2. When *dormant* is ``True``, uses
       :class:`~biff.relay.DormantRelay`.
    3. ``config.relay_url`` selects
       :class:`~biff.nats_relay.NatsRelay`.
    4. Otherwise :class:`~biff.relay.LocalRelay`.

    Runtime identity (tty, hostname, pwd) is auto-generated when not
    provided — each server instance gets a unique session key.
    """
    owns_relay = relay is None
    if relay is None:
        if dormant:
            relay = DormantRelay()
        elif config.relay_url:
            relay = NatsRelay(
                url=config.relay_url,
                auth=config.relay_auth,
                tls_handshake_first=config.relay_tls_handshake_first,
                name=f"biff-{config.repo_name}-{config.user}",
                repo_name=config.repo_name,
            )
        else:
            relay = LocalRelay(data_dir=data_dir)
    # Loader mirrors this fallback: ``get_repo_common_root == ""`` -> ``repo_root``.
    # A ``None`` from a test that never threaded the common root routes markers
    # through the shared ``hints/default`` bucket via ``Path("")``, matching the
    # fail-open contract in DES-054.
    resolved_common = repo_common_root or repo_root or Path()
    return ServerState(
        config=config,
        relay=relay,
        repo_common_root=resolved_common,
        tty=tty or generate_tty(),
        hostname=hostname or get_hostname(),
        pwd=pwd or get_pwd(),
        unread_path=unread_path,
        unread_dir=unread_dir,
        owns_relay=owns_relay,
        dormant=dormant,
        repo_root=repo_root,
        org_repos=org_repos or frozenset(),
    )
