"""Whose sessions doctor's channel check asks, and over which relay."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from biff.config import find_git_root, load_cli_config
from biff.identity import resolve_mcp_identity
from biff.nats_relay import NatsRelay
from biff.relay_select import RelaySelector

if TYPE_CHECKING:
    from pathlib import Path

    from biff.plugins.claude_code.channel_check_relay import CheckRelay

__all__ = ["CheckHandles", "CheckTarget"]


@final
class CheckHandles:
    """The handles a biff server in this repo would register sessions under."""

    __slots__ = ()

    @staticmethod
    def resolve(
        human: str, repo_root: Path, user_override: str | None
    ) -> tuple[str, ...]:
        """Return the server's handle (agent first), then *human* if it differs.

        Claude Code starts the MCP server, which registers under the agent
        identity chain (override or ``BIFF_USER``, the repo's agent, GitHub,
        OS user), not under the shell's human handle, so both are looked up.
        """
        try:
            server = resolve_mcp_identity(repo_root, user_override)[0]
        except SystemExit:  # no identity source: only the human handle remains
            server = ""
        return tuple(dict.fromkeys(h for h in (server, human) if h))


@dataclass(frozen=True, slots=True)
class CheckTarget:
    """Whose sessions to check and where; no relay means the check cannot run."""

    relay: CheckRelay | None  # absent when the relay is not NATS
    users: tuple[str, ...]

    @classmethod
    def from_environment(cls, user_override: str | None) -> Self:
        """Resolve the handles, repo and relay the way the CLI and the server do.

        *user_override* is ``--user``.  Without a repository or an identity, or
        with the local relay, there is no relay to ask.
        """
        try:
            resolved = load_cli_config(user_override=user_override)
        except SystemExit:
            return cls(None, ())
        relay = RelaySelector(resolved.config, resolved.data_dir).select(dormant=False)
        root = resolved.repo_root or find_git_root() or resolved.data_dir
        users = CheckHandles.resolve(resolved.config.user, root, user_override)
        return cls(relay if isinstance(relay, NatsRelay) else None, users)
