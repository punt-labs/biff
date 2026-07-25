"""Activity tracking for tool calls.

Every user-facing tool records a tool call on the shared
:class:`~biff.server.activity.ActivityTracker` so the background poller
stays out of napping while the agent is actively using biff.

This replaces the retired lazy-activation decorator: enablement is now
explicit repo policy (the committed ``.punt-labs/biff/enabled`` marker),
never auto-triggered on first tool use.  A dormant server registers its
tools against a :class:`~biff.relay.DormantRelay`, so a call in a
disabled repo is a harmless no-op -- it touches activity and returns
whatever the dormant relay yields.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TYPE_CHECKING, ParamSpec

if TYPE_CHECKING:
    from biff.server.state import ServerState

_P = ParamSpec("_P")


def track_activity(
    state: ServerState,
) -> Callable[
    [Callable[_P, Awaitable[str]]],
    Callable[_P, Awaitable[str]],
]:
    """Decorator: record a tool call on the activity tracker, then run.

    Usage inside a ``register()`` function::

        @mcp.tool(name="who", description="...")
        @track_activity(state)
        async def who() -> str:
            ...
    """

    def decorator(
        fn: Callable[_P, Awaitable[str]],
    ) -> Callable[_P, Awaitable[str]]:
        @wraps(fn)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> str:
            state.activity.touch()
            return await fn(*args, **kwargs)

        return wrapper

    return decorator
