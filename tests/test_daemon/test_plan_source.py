"""The ``plan`` method takes a ``source``: a hook's plan never beats a manual one."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from biff.client.connection import DaemonError
from biff.frame_reply import ErrorCode

if TYPE_CHECKING:
    from tests.test_daemon.conftest import Harness


def _reply(result: object) -> dict[str, object]:
    return cast("dict[str, object]", result)


async def test_auto_plan_keeps_a_manual_plan(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai", client="mcp")
    await kai.request("plan", message="mine")
    reply = _reply(await kai.request("plan", message="hook plan", source="auto"))
    assert reply["text"] == "Plan unchanged (manual):\n   mine"
    assert reply["data"] == {"plan": "mine", "unchanged": True}


async def test_auto_plan_is_visible_to_another_session(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai", client="mcp")
    eric = await harness.connect("s-eric", "eric")
    await kai.request("plan", message="hook plan", source="auto")
    rows = cast("list[dict[str, object]]", _reply(await eric.request("who"))["data"])
    assert {row["user"]: row["plan"] for row in rows}["kai"] == "hook plan"


async def test_a_manual_plan_replaces_an_auto_plan(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai", client="mcp")
    await kai.request("plan", message="hook plan", source="auto")
    reply = _reply(await kai.request("plan", message="mine"))
    assert reply["text"] == "Plan:\n   mine"


async def test_plan_without_a_source_is_manual(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai", client="mcp")
    await kai.request("plan", message="mine")
    reply = _reply(await kai.request("plan", message="hook plan", source="auto"))
    assert reply["data"] == {"plan": "mine", "unchanged": True}


async def test_an_unknown_source_is_refused(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai", client="mcp")
    with pytest.raises(DaemonError) as caught:
        await kai.request("plan", message="x", source="robot")
    assert caught.value.code is ErrorCode.INVALID_PARAMS
