"""Strip, validate, and bind the harness-declared ``_caller`` on every tool call."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from biff.caller_identity import CALLER_KEY, CallerIdentity, CallerIdentityError

if TYPE_CHECKING:
    from fastmcp.tools import ToolResult
    from mcp.types import CallToolRequestParams

# Entered around the whole tool call, so the server can keep one caller's
# session pointed at while its tool runs.
type CallerBinder = Callable[[CallerIdentity], AbstractAsyncContextManager[None]]

_UNIDENTIFIED = (
    f"call carried no {CALLER_KEY}: the biff stamping hook is not installed, "
    "is disabled (.punt-hooks-kill), or was killed before this call. "
    "Run `biff doctor`, then restart the session."
)


class CallerIdentityMiddleware(Middleware):
    """Remove ``_caller`` before schema validation, then run the call inside *bind*.

    fastmcp 3 runs ``on_call_tool`` before the tool's argument schema is
    checked, so the undeclared ``_caller`` is popped here and never reaches
    a tool.  A call without a well-formed ``_caller`` is refused: biff never
    falls back to guessing who is calling.
    """

    def __init__(self, bind: CallerBinder) -> None:
        self._bind = bind

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        arguments = context.message.arguments
        if arguments is None or CALLER_KEY not in arguments:
            raise ToolError(_UNIDENTIFIED)
        raw: object = arguments.pop(CALLER_KEY)
        try:
            caller = CallerIdentity.from_wire(raw)
        except CallerIdentityError as exc:
            raise ToolError(str(exc)) from exc
        async with self._bind(caller):
            return await call_next(context)
