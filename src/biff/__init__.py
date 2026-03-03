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

from importlib.metadata import version

from biff import commands
from biff.cli_session import CliContext
from biff.commands import CommandResult
from biff.config import load_config
from biff.models import BiffConfig, Message, UnreadSummary, UserSession, WallPost
from biff.nats_relay import NatsRelay
from biff.relay import DormantRelay, LocalRelay, Relay

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
