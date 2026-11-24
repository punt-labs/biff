"""Build a ``ServerState`` from configuration and the relay it selects."""

from __future__ import annotations

from pathlib import Path

from biff.models import BiffConfig
from biff.relay import Relay
from biff.relay_select import RelaySelector
from biff.server.state import RepoToggle, ServerState
from biff.tty import generate_tty, get_hostname, get_pwd


def create_state(
    config: BiffConfig,
    data_dir: Path,
    *,
    toggle: RepoToggle,
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
        relay = RelaySelector(config, data_dir).select(dormant=dormant)
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
        toggle=toggle,
    )
