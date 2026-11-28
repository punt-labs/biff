"""Which way ``biff mcp`` reaches the relay: through its own connection, or biffd."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["McpTransport"]

_ENV: Final = "BIFF_MCP_TRANSPORT"


class McpTransport(StrEnum):
    """The two ways the MCP server runs while both exist."""

    INPROCESS = "inprocess"
    DAEMON = "daemon"

    @classmethod
    def from_settings(
        cls, raw: Mapping[str, object], environ: Mapping[str, str]
    ) -> McpTransport:
        """Return the transport *environ* names, else the config's, else in-process.

        The environment variable ``BIFF_MCP_TRANSPORT`` wins over the config key
        ``mcp.transport``; an empty variable is absent.  A value that names
        neither transport is refused, because falling back would leave a person
        believing the daemon is in use.

        Raises:
            SystemExit: A setting names neither transport.
        """
        if named := environ.get(_ENV, ""):
            return cls._named(named, _ENV)
        section = raw.get("mcp")
        if section is None:
            return cls.INPROCESS
        if not isinstance(section, dict):
            msg = f"mcp must be a mapping with a transport key, got {section!r}"
            raise SystemExit(msg)
        settings = cast("dict[str, object]", section)
        return cls._named(
            settings.get("transport", cls.INPROCESS.value), "mcp.transport"
        )

    @classmethod
    def _named(cls, value: object, where: str) -> McpTransport:
        by_name = {t.value: t for t in cls}
        if isinstance(value, str) and value in by_name:
            return by_name[value]
        choices = ", ".join(repr(name) for name in by_name)
        msg = f"{where} must be one of {choices}, got {value!r}"
        raise SystemExit(msg)
