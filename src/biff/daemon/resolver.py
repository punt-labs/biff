"""Turn a hello into the configuration of the session it names."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, final

from biff.config import load_cli_config, load_mcp_config
from biff.daemon.relays import ResolvedSession

if TYPE_CHECKING:
    from biff.frames import Hello

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

    An agent harness (``mcp`` or ``plugin``) identifies as the agent; a shell
    or hook identifies as the human, as each does today.
    """

    async def resolve(self, hello: Hello) -> ResolvedSession:
        """Load the configuration for *hello* off the event loop.

        Raises:
            ResolveError: The directory is not a configured biff repository.
        """
        load = load_mcp_config if hello.client in ("mcp", "plugin") else load_cli_config
        try:
            resolved = await asyncio.to_thread(
                load, user_override=hello.user or None, start=Path(hello.cwd)
            )
        except SystemExit as exc:
            msg = f"cannot resolve biff configuration for {hello.cwd}: {exc.code}"
            raise ResolveError(msg) from exc
        return ResolvedSession(
            config=resolved.config,
            data_dir=resolved.data_dir,
            repo_root=resolved.repo_root,
        )
