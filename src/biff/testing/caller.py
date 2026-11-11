"""A test client that declares a ``_caller`` the way the stamping hook does."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

from fastmcp import Client

from biff.caller_identity import CALLER_KEY
from biff.session_routing import SessionRouting

if TYPE_CHECKING:
    import mcp.types
    from fastmcp.client.progress import ProgressHandler

    from biff.server.state import ServerState

# A main-thread caller: the stamping hook sends the main-thread agent id,
# which a test cannot corroborate, so the fixture states one directly.
DEFAULT_CALLER: dict[str, str] = {"session_id": "test-session", "agent_id": "4242"}

_KEEP = object()


class StampedClient(Client[Any]):  # pyright: ignore[reportMissingTypeArgument]
    """A :class:`fastmcp.Client` whose tool calls carry ``_caller``.

    ``caller`` defaults to :data:`DEFAULT_CALLER`.  Pass another mapping to
    act as a different session or a subagent, or ``None`` to send no
    ``_caller`` at all (the unidentified-call path).  A ``_caller`` already
    present in a call's arguments is never replaced.
    """

    def __init__(
        self,
        *args: Any,
        caller: dict[str, str] | object | None = _KEEP,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._caller = DEFAULT_CALLER if caller is _KEEP else caller

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        progress_handler: ProgressHandler | None = None,
        timeout: datetime.timedelta | float | int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> mcp.types.CallToolResult:
        """Send the call with ``_caller`` added unless the test supplied one."""
        if self._caller is not None and CALLER_KEY not in arguments:
            arguments = {**arguments, CALLER_KEY: self._caller}
        return await super().call_tool_mcp(
            name, arguments, progress_handler, timeout, meta
        )


def caller_for(session_id: str) -> dict[str, str]:
    """A main-thread ``_caller`` declaring *session_id*."""
    return {"session_id": session_id, "agent_id": "4242"}


def bound_to(state: ServerState, session_id: str = "test-session") -> ServerState:
    """Bind *state* to *session_id* ahead of its first call and return it.

    Binding names the session without registering it, so a test can read
    ``state.session_key`` before the first identified call attaches.
    """
    state.bind(SessionRouting(session_id))
    return state
