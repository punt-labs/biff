"""``create_state`` for tests: the real registry ``cli`` builds, never an empty one."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict, Unpack

from biff._cli_runtime import CliInvocation
from biff.models import BiffConfig
from biff.relay import Relay
from biff.server.state import ServerState
from biff.server.state_factory import StateBlueprint


class StateFields(TypedDict, total=False):
    """The optional fields of a ``StateBlueprint``."""

    relay: Relay | None
    unread_path: Path | None
    unread_dir: Path | None
    tty: str | None
    hostname: str | None
    pwd: str | None
    dormant: bool
    repo_root: Path | None
    repo_common_root: Path | None
    org_repos: frozenset[str] | None


def create_state(
    config: BiffConfig, data_dir: Path, **fields: Unpack[StateFields]
) -> ServerState:
    """Build a ``ServerState`` with the CLI's real repo toggle."""
    return StateBlueprint(
        config, data_dir, CliInvocation.repo_toggle(), **fields
    ).build()
