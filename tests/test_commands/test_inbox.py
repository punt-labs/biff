"""``Inbox.unread`` loses no mail when the second inbox cannot be fetched."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from biff.cli_context import CliContext
from biff.commands.inbox import Inbox
from biff.models import BiffConfig, Message
from biff.relay import LocalRelay

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence
    from pathlib import Path


class _ConsumingRelay(LocalRelay):
    """A relay whose fetch consumes mail, as the NATS relay's does.

    The broadcast inbox can be told to fail after the session inbox was fetched.
    """

    consumes_on_fetch = True

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.user_inbox_down = False

    async def fetch(self, session_key: str) -> list[Message]:
        unread = await super().fetch(session_key)
        await super().mark_read(session_key, [m.id for m in unread])
        return unread

    async def fetch_user_inbox(self, user: str) -> list[Message]:
        if self.user_inbox_down:
            msg = "the broadcast inbox is not answering"
            raise ConnectionError(msg)
        return await super().fetch_user_inbox(user)

    async def mark_read(self, session_key: str, ids: Sequence[uuid.UUID]) -> None:
        return None


@pytest.fixture
def consuming(tmp_path: Path) -> _ConsumingRelay:
    return _ConsumingRelay(tmp_path)


@pytest.fixture
def ctx(consuming: _ConsumingRelay) -> CliContext:
    return CliContext(
        relay=consuming,
        config=BiffConfig(user="kai", repo_name="test"),
        session_key="kai:abc12345",
        user="kai",
        tty="abc12345",
    )


async def test_session_mail_survives_a_broadcast_inbox_that_fails(
    ctx: CliContext, consuming: _ConsumingRelay
) -> None:
    await consuming.deliver(
        Message(from_user="eric", to_user="kai:abc12345", body="for the session")
    )
    consuming.user_inbox_down = True

    with pytest.raises(ConnectionError, match="not answering"):
        await Inbox.unread(ctx)
    consuming.user_inbox_down = False
    again = await Inbox.unread(ctx)

    assert [m.body for m in again.messages] == ["for the session"]
