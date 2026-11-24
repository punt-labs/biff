"""Build a ``ServerState`` from configuration and the relay it selects."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from biff.models import BiffConfig
from biff.relay import Relay
from biff.relay_select import RelaySelector
from biff.server.state import RepoToggle, ServerState
from biff.tty import generate_tty, get_hostname, get_pwd


@dataclass(frozen=True)
class StateBlueprint:
    """What a ``ServerState`` is built from; :meth:`build` creates it.

    Relay selection (in priority order):

    1. An explicit *relay* always wins (used by tests).
    2. When *dormant* is ``True``, uses
       :class:`~biff.relay.DormantRelay`.
    3. ``config.relay_url`` selects
       :class:`~biff.nats_relay.NatsRelay`.
    4. Otherwise :class:`~biff.relay.LocalRelay`.

    Runtime identity (tty, hostname, pwd) is auto-generated when not
    provided: each server instance gets a unique session key.
    """

    config: BiffConfig
    data_dir: Path
    toggle: RepoToggle
    relay: Relay | None = None
    unread_path: Path | None = None
    unread_dir: Path | None = None
    tty: str | None = None
    hostname: str | None = None
    pwd: str | None = None
    dormant: bool = False
    repo_root: Path | None = None
    repo_common_root: Path | None = None
    org_repos: frozenset[str] | None = None

    def build(self) -> ServerState:
        """Create the ``ServerState`` this blueprint describes."""
        tty, hostname, pwd = self._identity()
        return ServerState(
            config=self.config,
            relay=self._selected_relay(),
            repo_common_root=self._common_root(),
            tty=tty,
            hostname=hostname,
            pwd=pwd,
            unread_path=self.unread_path,
            unread_dir=self.unread_dir,
            owns_relay=self.relay is None,
            dormant=self.dormant,
            repo_root=self.repo_root,
            org_repos=self.org_repos or frozenset(),
            toggle=self.toggle,
        )

    def _selected_relay(self) -> Relay:
        """Return the explicit relay, or the one the configuration selects."""
        return self.relay or RelaySelector(self.config, self.data_dir).select(
            dormant=self.dormant
        )

    def _common_root(self) -> Path:
        """Return the repo's common root, falling back as the loader does.

        Loader mirrors this fallback: ``get_repo_common_root == ""`` ->
        ``repo_root``.  A ``None`` from a test that never threaded the common
        root routes markers through the shared ``hints/default`` bucket via
        ``Path("")``, matching the fail-open contract in DES-054.
        """
        return self.repo_common_root or self.repo_root or Path()

    def _identity(self) -> tuple[str, str, str]:
        """Return ``(tty, hostname, pwd)``, generated where not given."""
        return (
            self.tty or generate_tty(),
            self.hostname or get_hostname(),
            self.pwd or get_pwd(),
        )
