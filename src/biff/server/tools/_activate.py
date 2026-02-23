"""Lazy activation — auto-enable biff on first tool use while dormant.

When biff starts in dormant mode (no ``.biff.local`` with ``enabled = true``),
calling any tool is treated as intent to use biff.  The :func:`auto_enable`
decorator wraps a tool so that dormant invocations write the activation
config to disk and return a restart message instead of running the tool.

The ``biff`` toggle tool is the one exception: it handles its own
enable/disable logic and must NOT use ``auto_enable``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TYPE_CHECKING, ParamSpec

from biff.config import (
    ensure_biff_file,
    ensure_gitignore,
    write_biff_local,
)

if TYPE_CHECKING:
    from biff.server.state import ServerState

_P = ParamSpec("_P")


def lazy_activate(state: ServerState) -> str | None:
    """Auto-enable biff on first tool use while dormant.

    Returns an activation message if the server was dormant (caller
    should return this string).  Returns ``None`` if already active.
    """
    if not state.dormant:
        return None

    repo_root = state.repo_root
    if repo_root is None:
        return "biff: not in a git repository."

    ensure_biff_file(
        repo_root, team=state.config.team, relay_url=state.config.relay_url
    )

    write_biff_local(repo_root, enabled=True)
    ensure_gitignore(repo_root)

    return "biff enabled. Restart Claude Code to connect."


def auto_enable(
    state: ServerState,
) -> Callable[
    [Callable[_P, Awaitable[str]]],
    Callable[_P, Awaitable[str]],
]:
    """Decorator: auto-enable biff when a tool is called while dormant.

    Wrap every tool (except ``biff`` toggle) so that dormant invocations
    write ``.biff.local`` and return a restart prompt instead of running
    the tool body.

    Usage inside a ``register()`` function::

        @mcp.tool(name="who", description="...")
        @auto_enable(state)
        async def who() -> str:
            ...
    """

    def decorator(
        fn: Callable[_P, Awaitable[str]],
    ) -> Callable[_P, Awaitable[str]]:
        @wraps(fn)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> str:
            msg = lazy_activate(state)
            if msg:
                return msg
            return await fn(*args, **kwargs)

        return wrapper

    return decorator
