"""Configure diagnostics for Biff entry points."""

from __future__ import annotations

from functools import partial

from punt_observe.logging import configure_logging as _configure_logging

configure_logging = partial(
    _configure_logging,
    "biff",
    quiet=("mcp", "fastmcp", "nats", "httpx", "httpcore", "urllib3", "asyncio"),
)
