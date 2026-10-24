"""Reject tool calls from any caller other than the explicitly pinned session."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

if TYPE_CHECKING:
    from fastmcp.tools import ToolResult
    from mcp.types import CallToolRequestParams

from biff.ethos_session import EthosSessionPin


class SessionPinMiddleware(Middleware):
    """Validate OpenCode metadata before dispatch, with no identity rebinding."""

    def __init__(self, pin: EthosSessionPin) -> None:
        self.pin = pin

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        # FastMCP reconstructs middleware params from name/arguments and drops
        # wire metadata. Only the transport request context is authoritative.
        request = (
            context.fastmcp_context.request_context
            if context.fastmcp_context is not None
            else None
        )
        meta = request.meta if request is not None else None
        caller: object = (
            meta.model_dump().get("ai.opencode/sessionID") if meta is not None else None
        )
        if not isinstance(caller, str) or caller != self.pin.session_id:
            raise ToolError(
                "Biff is pinned to another OpenCode session or caller metadata is "
                "missing/invalid; ai.opencode/sessionID must match --ethos-session."
            )
        return await call_next(context)
