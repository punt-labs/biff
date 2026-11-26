"""Turn a hello into the configuration of the session it names."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, final

import yaml

from biff.config import load_cli_config, load_mcp_config
from biff.daemon.relays import ResolvedSession

if TYPE_CHECKING:
    from biff.frame_client import Hello

__all__ = ["DiskResolver", "ResolveError", "SessionResolver"]


class ResolveError(ValueError):
    """A hello's directory has no usable biff configuration."""


class SessionResolver(Protocol):
    """Maps a hello to the session configuration its client runs under."""

    async def resolve(self, hello: Hello) -> ResolvedSession:
        """Return the configuration for *hello*.

        Raises:
            ResolveError: The configuration cannot be resolved.
        """
        ...


@final
class DiskResolver:
    """Reads the repository configuration under the hello's working directory.

    Every client of an agent harness (``mcp``, ``plugin`` and the harness's
    ``hook``) identifies as the agent, so the hook and the server of one session
    name the same user.  Only a shell (``cli``) identifies as the human.
    """

    async def resolve(self, hello: Hello) -> ResolvedSession:
        """Load the configuration for *hello* off the event loop.

        Raises:
            ResolveError: The directory is not a configured biff repository, or
                its configuration cannot be read or parsed.
        """
        agent_side = hello.client in ("mcp", "plugin", "hook")
        load = load_mcp_config if agent_side else load_cli_config
        try:
            resolved = await asyncio.to_thread(
                load, user_override=hello.user or None, start=Path(hello.cwd)
            )
        except SystemExit as exc:
            detail = "no usable configuration" if exc.code is None else exc.code
            msg = f"cannot resolve biff configuration for {hello.cwd}: {detail}"
            raise ResolveError(msg) from exc
        except (OSError, yaml.YAMLError, ValueError) as exc:  # ValueError: pydantic
            msg = f"cannot resolve biff configuration for {hello.cwd}: {exc}"
            raise ResolveError(msg) from exc
        return ResolvedSession(
            config=resolved.config,
            data_dir=resolved.data_dir,
            repo_root=resolved.repo_root,
        )
