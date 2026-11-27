"""Each product command prints the same through biffd as it does in process."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from biff import commands
from biff.cli_context import CliContext
from biff.cli_daemon_wire import WireResult
from biff.commands.inbox import Inbox
from biff.formatting import format_read
from biff.models import BiffConfig
from biff.relay import LocalRelay
from biff.session_id import CLI_ROLE
from biff.session_routing import SessionRouting

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pydantic import JsonValue

    from biff.client.connection import DaemonClient
    from tests.test_daemon.conftest import Harness

type InProcess = Callable[[CliContext], Awaitable[commands.CommandResult]]


async def _both(harness: Harness) -> tuple[DaemonClient, CliContext]:
    """One daemon client and an in-process context for the same session."""
    client = await harness.connect("s-parity", "kai")
    status = cast("dict[str, dict[str, str]]", await client.request("status"))
    routing = SessionRouting("s-parity", role=CLI_ROLE)
    ctx = CliContext(
        relay=LocalRelay(harness.factory.built[0]),
        config=BiffConfig(user="kai", repo_name="test"),
        session_key=routing.session_key("kai"),
        user="kai",
        tty=routing.routing_id,
        tty_name=status["data"]["tty_name"],
    )
    return client, ctx


_TABLE: list[tuple[str, dict[str, JsonValue], InProcess]] = [
    ("who", {}, commands.who),
    ("finger", {"user": "kai"}, lambda ctx: commands.finger(ctx, "kai")),
    (
        "write",
        {"to": "kai", "message": "hi"},
        lambda ctx: commands.write(ctx, "kai", "hi"),
    ),
    ("plan", {"message": "parity"}, lambda ctx: commands.plan(ctx, "parity")),
    ("last", {"user": "", "count": 25}, lambda ctx: commands.last(ctx, "", 25)),
    (
        "wall",
        {"message": "freeze", "duration": "1h", "clear": False},
        lambda ctx: commands.wall(ctx, "freeze", "1h", clear=False),
    ),
    (
        "wall",
        {"message": "", "duration": "", "clear": False},
        lambda ctx: commands.wall(ctx, "", "", clear=False),
    ),
    ("mesg", {"value": "y"}, lambda ctx: commands.mesg(ctx, "y")),
    ("tty", {"name": "review"}, lambda ctx: commands.tty(ctx, "review")),
    ("status", {}, commands.status),
    ("finger", {"user": "nobody"}, lambda ctx: commands.finger(ctx, "nobody")),
    ("mesg", {"value": "maybe"}, lambda ctx: commands.mesg(ctx, "maybe")),
]


@pytest.mark.parametrize(
    ("method", "params", "in_process"),
    _TABLE,
    ids=[f"{m}-{i}" for i, (m, _, _) in enumerate(_TABLE)],
)
async def test_a_command_prints_and_fails_the_same_through_biffd(
    harness: Harness,
    method: str,
    params: dict[str, JsonValue],
    in_process: InProcess,
) -> None:
    client, ctx = await _both(harness)
    expected = await in_process(ctx)
    wire = cast("dict[str, JsonValue]", await client.request(method, **params))
    assert (wire["text"], wire["error"]) == (expected.text, expected.error)


async def test_read_prints_the_same_through_biffd(harness: Harness) -> None:
    client, ctx = await _both(harness)
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to="kai", message="same words")
    wire = cast("dict[str, JsonValue]", await client.request("read"))
    await eric.request("write", to="kai", message="same words")
    expected = await commands.read(ctx)
    assert wire["text"] == expected.text


async def test_read_with_a_backlog_prints_one_batch_and_says_how_much_is_left(
    harness: Harness,
) -> None:
    client, ctx = await _both(harness)
    eric = await harness.connect("s-eric", "eric")
    for n in range(300):
        await eric.request("write", to="kai", message=f"letter {n:03d} " + "x" * 100)
    waiting = (await Inbox.unread(ctx)).messages
    wire = cast("dict[str, JsonValue]", await client.request("read"))
    shown = len(cast("list[JsonValue]", wire["data"]))
    result = WireResult.parse(wire).command_result()
    left = len(waiting) - shown
    assert 0 < shown < 300
    assert result.text == (
        f"{format_read(waiting[:shown])}\n{left} more unread; run biff read again"
    )
