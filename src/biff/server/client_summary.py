"""Which client connected, as its ``initialize`` request says (DES-087).

A channel event is a notification: the client sends no reply, so biff cannot
see whether Claude Code showed it.  What biff can record is who connected and
which experimental capabilities that client declared, which is where a client
would say it accepts ``claude/channel`` events.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

_UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ClientSummary:
    """A client's name, version and experimental capability names."""

    name: str
    version: str
    experimental: tuple[str, ...]

    @classmethod
    def from_initialize(cls, params: Mapping[str, object] | None) -> Self:
        """Read *params* of an ``initialize`` request; absent fields are unknown.

        ``params`` is wire data, so every field is narrowed before use; a
        missing or malformed field reads as unknown rather than failing the
        handshake.
        """
        request = cls._mapping(params)
        info = cls._mapping(request.get("clientInfo"))
        capabilities = cls._mapping(request.get("capabilities"))
        experimental = cls._mapping(capabilities.get("experimental"))
        return cls(
            name=cls._text(info.get("name")),
            version=cls._text(info.get("version")),
            experimental=tuple(sorted(experimental)),
        )

    @staticmethod
    def _mapping(value: object) -> Mapping[str, object]:
        """Return *value* as a string-keyed mapping, or an empty one."""
        if not isinstance(value, dict):
            return {}
        raw = cast("dict[object, object]", value)
        return {str(key): item for key, item in raw.items()}

    @staticmethod
    def _text(value: object) -> str:
        """Return *value* when it is a non-empty string, else ``unknown``."""
        return value if isinstance(value, str) and value else _UNKNOWN

    def __str__(self) -> str:
        """Render ``client=name/version experimental=a,b`` for the log."""
        declared = ",".join(self.experimental) or "none"
        return f"client={self.name}/{self.version} experimental={declared}"

    def handshake(self, protocol_version: str) -> str:
        """Return the log line for a channel-mode handshake at *protocol_version*."""
        return (
            f"MCP handshake: protocolVersion={protocol_version} channel_mode=on {self}"
        )
