"""Work a tool leaves until its result has been produced."""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING, Final, final

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from biff.client.connection import DaemonError
from biff.server.mcp_link import FAILURES

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastmcp.tools import ToolResult
    from mcp.types import CallToolRequestParams

__all__ = ["AfterCall"]

logger = logging.getLogger(__name__)

type Action = Callable[[], Awaitable[None]]

_ACTIONS: Final[ContextVar[list[Action] | None]] = ContextVar(
    "biff_after_call", default=None
)


@final
class AfterCall(Middleware):
    """Runs what a tool deferred once the tool has returned, in order.

    A tool that read mail from biffd defers the confirmation, so biffd marks the
    mail read only after the tool produced the text that carries it.  A tool
    that raises defers nothing that runs: the mail stays unread and the next
    call serves it again.  An action that fails is logged and the result still
    goes out; what it left undone is retried by the next call.
    """

    @staticmethod
    def defer(action: Action) -> None:
        """Run *action* after the current tool call returns.

        Raises:
            RuntimeError: Called outside a tool call this middleware wraps.
        """
        actions = _ACTIONS.get()
        if actions is None:
            msg = "defer() was called outside a tool call"
            raise RuntimeError(msg)
        actions.append(action)

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Run the tool, then what it deferred."""
        actions: list[Action] = []
        token = _ACTIONS.set(actions)
        try:
            result = await call_next(context)
        finally:
            _ACTIONS.reset(token)
        for action in actions:
            try:
                await action()
            except (*FAILURES, DaemonError) as exc:
                logger.warning("work after a tool call failed: %s", exc)
        return result
