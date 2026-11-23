"""Test helper: the fields one config dict yields, as a flat tuple."""

from __future__ import annotations

from biff.config import _extract_peers, _extract_team  # pyright: ignore[reportPrivateUsage]
from biff.models import RelayAuth
from biff.relay_config import RelayLayers


def extract_biff_fields(
    raw: dict[str, object],
) -> tuple[
    tuple[str, ...],
    str | None,
    RelayAuth | None,
    bool,
    tuple[str, ...],
    tuple[str, ...],
]:
    """Return team, relay url, relay auth, relay TLS mode, peers and orgs."""
    relay = RelayLayers.of([("config", raw)]).resolve()
    peers, orgs = _extract_peers(raw)
    return (
        _extract_team(raw),
        relay.url,
        relay.auth,
        relay.tls_handshake_first,
        peers,
        orgs,
    )
