"""Activity tracking and the dormant-repo guard for tool calls.

Every user-facing tool records a tool call on the shared
:class:`~biff.server.activity.ActivityTracker` so the background poller
stays out of napping while the agent is actively using biff.

This replaces the retired lazy-activation decorator: enablement is now
explicit repo policy (the committed ``.punt-labs/biff/enabled`` marker),
never auto-triggered on first tool use.  When the server is dormant (the
marker is absent), a decorated tool does NOT run its body and does NOT
write anything -- it returns :data:`_DISABLED_NOTICE`, a concise,
actionable line telling the agent how to turn biff on.  A silent no-op
would be a silent failure; the notice is the response to one explicit
tool call, so it is emitted once per call, not on a loop.

The enable/disable, relay, and poll-config tools are deliberately NOT
decorated with :func:`track_activity` -- they must work while dormant so
``/biff enable`` can turn biff on in the first place.

Every tool body decorated here that touches session state does so via
:func:`~biff.server.tools._session.update_current_session`, which is the
single writer of ``UserSession.last_tool_at`` -- the timestamp ``/who``
and ``/finger`` display as idle time.  ``track_activity`` marks
the boundary of a real tool call; ``update_current_session`` persists it.
The pairing is what keeps ``last_tool_at`` distinct from ``last_active``,
which the background heartbeat refreshes unconditionally every tick.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TYPE_CHECKING, ParamSpec

if TYPE_CHECKING:
    from biff.server.state import ServerState

_P = ParamSpec("_P")

_DISABLED_NOTICE = (
    "biff is disabled in this repo. Run `biff enable` (or /biff enable), "
    "then restart Claude Code."
)


def track_activity(
    state: ServerState,
) -> Callable[
    [Callable[_P, Awaitable[str]]],
    Callable[_P, Awaitable[str]],
]:
    """Decorator: guard on enablement, then record the call and run.

    When *state* is dormant (biff disabled for this repo), the wrapped
    tool returns :data:`_DISABLED_NOTICE` without running its body or
    writing anything.  Otherwise it records the call on the activity
    tracker and runs the body.

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
            if state.dormant:
                return _DISABLED_NOTICE
            state.activity.touch()
            return await fn(*args, **kwargs)

        return wrapper

    return decorator
