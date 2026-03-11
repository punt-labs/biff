"""Biff: The dog that barked when messages arrived.

A modern CLI communication tool for software engineers,
resurrecting the UNIX communication vocabulary as MCP-native
slash commands for team collaboration.

Library API — import core types for programmatic use::

    from biff import BiffConfig, Message, UserSession, NatsRelay
    from biff import commands, load_config
    result = await commands.who(ctx)
"""

from __future__ import annotations

import importlib
from importlib.metadata import version
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from biff import commands as commands
    from biff.cli_session import CliContext as CliContext
    from biff.commands import CommandResult as CommandResult
    from biff.config import load_config as load_config
    from biff.models import (
        BiffConfig as BiffConfig,
        Message as Message,
        UnreadSummary as UnreadSummary,
        UserSession as UserSession,
        WallPost as WallPost,
    )
    from biff.nats_relay import NatsRelay as NatsRelay
    from biff.relay import (
        DormantRelay as DormantRelay,
        LocalRelay as LocalRelay,
        Relay as Relay,
    )

__version__ = version("punt-biff")

__all__ = [
    "BiffConfig",
    "CliContext",
    "CommandResult",
    "DormantRelay",
    "LocalRelay",
    "Message",
    "NatsRelay",
    "Relay",
    "UnreadSummary",
    "UserSession",
    "WallPost",
    "__version__",
    "commands",
    "load_config",
]


def __getattr__(name: str) -> object:
    """Lazy import for public API symbols.

    Avoids loading the full dependency tree (nats, pydantic, fastmcp)
    when lightweight entry points like ``biff-hook`` only need
    ``biff._stdlib`` or ``biff.hook``.
    """
    # Module re-exports (import the submodule itself).
    submodules = {"commands"}
    if name in submodules:
        mod = importlib.import_module(f"biff.{name}")
        globals()[name] = mod
        return mod

    # Attribute re-exports (import from a submodule).
    attrs: dict[str, tuple[str, str]] = {
        "CliContext": ("biff.cli_session", "CliContext"),
        "CommandResult": ("biff.commands", "CommandResult"),
        "load_config": ("biff.config", "load_config"),
        "BiffConfig": ("biff.models", "BiffConfig"),
        "Message": ("biff.models", "Message"),
        "UnreadSummary": ("biff.models", "UnreadSummary"),
        "UserSession": ("biff.models", "UserSession"),
        "WallPost": ("biff.models", "WallPost"),
        "NatsRelay": ("biff.nats_relay", "NatsRelay"),
        "DormantRelay": ("biff.relay", "DormantRelay"),
        "LocalRelay": ("biff.relay", "LocalRelay"),
        "Relay": ("biff.relay", "Relay"),
    }
    if name in attrs:
        module_path, attr = attrs[name]
        mod = importlib.import_module(module_path)
        value = getattr(mod, attr)
        globals()[name] = value  # cache for subsequent access
        return value
    msg = f"module 'biff' has no attribute {name!r}"
    raise AttributeError(msg)
