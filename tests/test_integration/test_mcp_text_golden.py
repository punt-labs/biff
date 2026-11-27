"""Every MCP tool returns, byte for byte, the text the display pipeline expects.

The panel line and the model's context are cut from a tool's text by
``plugin/hooks/suppress-output.sh``, so one changed character can break a
column count or a prefix match.  This is the oracle that any other way of
serving the tools (a thin client of biffd) must reproduce.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastmcp.tools.function_tool import FunctionTool

from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.tools import register_channel_tools
from biff.server.tools.channel_ack import NOT_ACCEPTED
from biff.testing import RecordingClient
from tests._mcp_golden import STEPS, Step, normalise
from tests._state import create_state

if TYPE_CHECKING:
    from pathlib import Path


def _differences(steps: tuple[Step, ...], actual: list[str]) -> list[str]:
    """Name each step whose normalised text is not its golden text."""
    return [
        f"{step.user} {step.tool} {step.args}:\n  want {step.text!r}\n  got  {got!r}"
        for step, text in zip(steps, actual, strict=True)
        if (got := normalise(text, step.volatile)) != step.text
    ]


async def test_every_tool_returns_its_golden_text(
    kai: RecordingClient, eric: RecordingClient
) -> None:
    clients = {"kai": kai, "eric": eric}
    actual = [await clients[s.user].call(s.tool, **s.args) for s in STEPS]

    assert _differences(STEPS, actual) == []


@pytest.mark.parametrize("index", range(len(STEPS)))
def test_a_one_character_change_in_any_golden_text_is_caught(index: int) -> None:
    step = STEPS[index]
    changed = step.text[:-1] + ("." if step.text[-1] != "." else "!")

    assert _differences((step,), [changed]) != []


async def test_channel_ack_refuses_a_token_it_never_issued(tmp_path: Path) -> None:
    state = create_state(
        BiffConfig(user="kai", repo_name="_test-golden"),
        tmp_path,
        tty="tty1",
        hostname="h",
        pwd="/p",
        repo_root=tmp_path,
        unread_path=tmp_path / "unread.json",
    )
    mcp = create_server(state)
    register_channel_tools(mcp, state)
    tool = await mcp.get_tool("channel_ack")
    assert isinstance(tool, FunctionTool)

    assert (
        str(await tool.fn(token="nope"))
        == NOT_ACCEPTED
        == ("biff channel token not accepted; nothing changed.")
    )
