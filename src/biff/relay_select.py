"""Choose the relay a process uses from its configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.nats_relay import NatsRelay
from biff.relay import DormantRelay, LocalRelay, Relay

if TYPE_CHECKING:
    from pathlib import Path

    from biff.models import BiffConfig

__all__ = ["select_relay"]


def select_relay(config: BiffConfig, data_dir: Path, *, dormant: bool) -> Relay:
    """Return the relay *config* selects.

    A *dormant* repo gets :class:`~biff.relay.DormantRelay`; otherwise
    ``config.relay_url`` selects :class:`~biff.nats_relay.NatsRelay`, and
    without one a :class:`~biff.relay.LocalRelay` under *data_dir*.
    """
    if dormant:
        return DormantRelay()
    if config.relay_url:
        return NatsRelay(
            url=config.relay_url,
            auth=config.relay_auth,
            tls_handshake_first=config.relay_tls_handshake_first,
            name=f"biff-{config.repo_name}-{config.user}",
            repo_name=config.repo_name,
        )
    return LocalRelay(data_dir=data_dir)
