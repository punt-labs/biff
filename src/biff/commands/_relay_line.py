"""How ``biff status`` names the relay a session is on."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from biff.relay import LocalRelay, Relay


@dataclass(frozen=True)
class RelayLine:
    """The relay's short name, the directory it keeps files in, and its status line."""

    name: str
    directory: str | None
    text: str

    @classmethod
    def of(cls, relay: Relay, url: str | None) -> Self:
        """Describe *relay*: its NATS *url*, or the local relay's directory."""
        if url:
            return cls(url, None, f"relay: {url} (connected)")
        directory = str(relay.data_dir) if isinstance(relay, LocalRelay) else None
        return cls("local", directory, f"relay: local ({directory})")
