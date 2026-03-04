"""Multi-user interaction tests.

These tests use both ``ctx`` (kai) and ``ctx_eric`` (eric) fixtures
against the same ``LocalRelay`` to verify cross-user scenarios:
message delivery, wall visibility, session isolation, and the
full write→read round-trip.
"""

from __future__ import annotations

from typing import cast

from biff.cli_session import CliContext
from biff.commands.finger import finger
from biff.commands.mesg import mesg
from biff.commands.plan import plan
from biff.commands.read import read
from biff.commands.status import status
from biff.commands.wall import wall
from biff.commands.who import who
from biff.commands.write import write
from biff.relay import LocalRelay


class TestWriteReadRoundTrip:
    async def test_kai_writes_eric_reads(
        self, ctx: CliContext, ctx_eric: CliContext
    ) -> None:
        result = await write(ctx, "@eric", "Hey eric!")
        assert not result.error

        result = await read(ctx_eric)
        assert not result.error
        assert "Hey eric!" in result.text
        assert "kai" in result.text
        data = cast("list[dict[str, object]]", result.json_data)
        assert len(data) == 1

    async def test_read_marks_messages_consumed(
        self, ctx: CliContext, ctx_eric: CliContext
    ) -> None:
        await write(ctx, "@eric", "first")
        await write(ctx, "@eric", "second")

        # Eric reads both
        result = await read(ctx_eric)
        data = cast("list[dict[str, object]]", result.json_data)
        assert len(data) == 2

        # Second read should be empty
        result2 = await read(ctx_eric)
        assert result2.json_data == []

    async def test_messages_isolated_per_user(
        self, ctx: CliContext, ctx_eric: CliContext
    ) -> None:
        await write(ctx, "@eric", "for eric only")

        # Kai shouldn't see eric's messages
        kai_read = await read(ctx)
        assert kai_read.json_data == []

        # Eric should see the message
        eric_read = await read(ctx_eric)
        data = cast("list[dict[str, object]]", eric_read.json_data)
        assert len(data) == 1

    async def test_bidirectional_messaging(
        self, ctx: CliContext, ctx_eric: CliContext
    ) -> None:
        await write(ctx, "@eric", "ping")
        await write(ctx_eric, "@kai", "pong")

        kai_msgs = await read(ctx)
        eric_msgs = await read(ctx_eric)

        kai_data = cast("list[dict[str, object]]", kai_msgs.json_data)
        eric_data = cast("list[dict[str, object]]", eric_msgs.json_data)
        assert len(kai_data) == 1
        assert kai_data[0]["body"] == "pong"
        assert len(eric_data) == 1
        assert eric_data[0]["body"] == "ping"


class TestWallVisibility:
    async def test_kai_posts_eric_sees(
        self, ctx: CliContext, ctx_eric: CliContext
    ) -> None:
        post_result = await wall(ctx, "deploy freeze", "1h", clear=False)
        assert not post_result.error

        # Eric reads the wall
        eric_result = await wall(ctx_eric, "", "", clear=False)
        assert not eric_result.error
        assert "deploy freeze" in eric_result.text
        assert "kai" in eric_result.text

    async def test_kai_clears_eric_sees_empty(
        self, ctx: CliContext, ctx_eric: CliContext
    ) -> None:
        await wall(ctx, "temp wall", "1h", clear=False)
        await wall(ctx, "", "", clear=True)

        eric_result = await wall(ctx_eric, "", "", clear=False)
        assert eric_result.text == "No active wall."

    async def test_wall_overwrites(self, ctx: CliContext, ctx_eric: CliContext) -> None:
        await wall(ctx, "first wall", "1h", clear=False)
        await wall(ctx_eric, "eric wall", "30m", clear=False)

        # Last writer wins
        result = await wall(ctx, "", "", clear=False)
        assert "eric wall" in result.text


class TestSessionVisibility:
    async def test_who_shows_both_users(
        self, ctx: CliContext, ctx_eric: CliContext, relay: LocalRelay
    ) -> None:
        await plan(ctx, "coding biff")
        await plan(ctx_eric, "reviewing PRs")

        result = await who(ctx)
        assert not result.error
        assert "kai" in result.text
        assert "eric" in result.text
        assert "coding biff" in result.text
        assert "reviewing PRs" in result.text

    async def test_finger_sees_other_user(
        self, ctx: CliContext, ctx_eric: CliContext
    ) -> None:
        await plan(ctx_eric, "deep work")

        result = await finger(ctx, "@eric")
        assert not result.error
        assert "deep work" in result.text

    async def test_status_isolated(self, ctx: CliContext, ctx_eric: CliContext) -> None:
        # Send messages only to eric
        await write(ctx, "@eric", "hey")

        kai_status = await status(ctx)
        eric_status = await status(ctx_eric)

        kai_data = cast("dict[str, object]", kai_status.json_data)
        eric_data = cast("dict[str, object]", eric_status.json_data)

        assert kai_data["user"] == "kai"
        assert eric_data["user"] == "eric"
        # Eric should have unread, kai should not
        assert eric_data["unread"] != 0


class TestMesgIsolation:
    async def test_mesg_per_user(
        self, ctx: CliContext, ctx_eric: CliContext, relay: LocalRelay
    ) -> None:
        await mesg(ctx, "off")
        await mesg(ctx_eric, "on")

        kai_session = await relay.get_session("kai:abc12345")
        eric_session = await relay.get_session("eric:def67890")

        assert kai_session is not None
        assert not kai_session.biff_enabled
        assert eric_session is not None
        assert eric_session.biff_enabled
