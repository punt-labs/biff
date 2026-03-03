"""Biff: The dog that barked when messages arrived.

A modern CLI communication tool for software engineers,
resurrecting the UNIX communication vocabulary as MCP-native
slash commands for team collaboration.

Library API — import core types for programmatic use::

    from biff import BiffConfig, Message, UserSession, NatsRelay
    from biff import load_config
"""

from __future__ import annotations

from importlib.metadata import version

from biff.config import load_config
from biff.models import BiffConfig, Message, UnreadSummary, UserSession, WallPost
from biff.nats_relay import NatsRelay
from biff.relay import DormantRelay, LocalRelay, Relay

__version__ = version("punt-biff")

__all__ = [
    "BiffConfig",
    "DormantRelay",
    "LocalRelay",
    "Message",
    "NatsRelay",
    "Relay",
    "UnreadSummary",
    "UserSession",
    "WallPost",
    "__version__",
    "load_config",
]
