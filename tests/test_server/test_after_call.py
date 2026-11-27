"""``AfterCall`` runs what a tool deferred, only once the tool has returned."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

import pytest

from biff.server.after_call import AfterCall

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


async def _through(
    tool: Callable[[], Awaitable[str]],
) -> str:
    """Run *tool* the way FastMCP does: as the ``call_next`` of the middleware."""

    async def call_next(_context: object) -> str:
        return await tool()

    middleware = cast("Any", AfterCall())  # pyright: ignore[reportExplicitAny]
    return str(await middleware.on_call_tool(object(), call_next))


async def test_what_a_tool_deferred_runs_after_it_returned_in_order() -> None:
    order: list[str] = []

    async def first() -> None:
        order.append("first")

    async def second() -> None:
        order.append("second")

    async def tool() -> str:
        AfterCall.defer(first)
        AfterCall.defer(second)
        order.append("tool returned")
        return "text"

    result = await _through(tool)

    assert (result, order) == ("text", ["tool returned", "first", "second"])


async def test_a_tool_that_raises_leaves_what_it_deferred_undone() -> None:
    ran: list[str] = []

    async def action() -> None:
        ran.append("action")

    async def tool() -> str:
        AfterCall.defer(action)
        msg = "the tool failed"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="the tool failed"):
        await _through(tool)

    assert ran == []


async def test_an_action_that_fails_is_logged_and_the_result_still_goes_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def action() -> None:
        msg = "biffd went away"
        raise ConnectionError(msg)

    async def tool() -> str:
        AfterCall.defer(action)
        return "text"

    with caplog.at_level(logging.WARNING, logger="biff.server.after_call"):
        result = await _through(tool)

    assert (result, "biffd went away" in caplog.text) == ("text", True)


def test_deferring_outside_a_tool_call_is_refused() -> None:
    async def action() -> None:
        return None

    with pytest.raises(RuntimeError, match="outside a tool call"):
        AfterCall.defer(action)
