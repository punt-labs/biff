"""Tests for ``biff.commands.read``."""

from __future__ import annotations

from typing import cast

from biff.cli_session import CliContext
from biff.commands.read import read
from biff.models import Message
from biff.relay import LocalRelay


class TestRead:
    async def test_empty_inbox(self, ctx: CliContext) -> None:
        result = await read(ctx)
        assert not result.error
        assert result.text == "No new messages."
        assert result.json_data == []

    async def test_reads_and_marks(self, ctx: CliContext, relay: LocalRelay) -> None:
        # Deliver a broadcast message to kai
        msg = Message(from_user="eric", to_user="kai", body="Hey kai")
        await relay.deliver(msg, sender_key="eric:def67890")

        result = await read(ctx)
        assert not result.error
        assert "eric" in result.text
        assert "Hey kai" in result.text
        data = cast("list[dict[str, object]]", result.json_data)
        assert len(data) == 1

        # Second read should be empty (messages marked read)
        result2 = await read(ctx)
        assert result2.text == "No new messages."

    async def test_reads_tty_and_user_inbox(
        self, ctx: CliContext, relay: LocalRelay
    ) -> None:
        # Broadcast to user
        await relay.deliver(
            Message(from_user="eric", to_user="kai", body="broadcast"),
            sender_key="eric:def67890",
        )
        # Targeted to tty
        await relay.deliver(
            Message(from_user="eric", to_user="kai:abc12345", body="targeted"),
            sender_key="eric:def67890",
        )

        result = await read(ctx)
        assert not result.error
        data = cast("list[dict[str, object]]", result.json_data)
        assert len(data) == 2
