"""A stored message over a field bound is surfaced loudly and kept, never dropped."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from biff.models import Message
from biff.relay import LocalRelay
from biff.tty import build_session_key

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


async def test_the_longest_legal_addresses_round_trip(tmp_path: Path) -> None:
    relay = LocalRelay(tmp_path)
    user, session_id = "u" * 128, "a" * 64  # a routing id at its longest
    message = Message(
        from_user=user,
        from_tty=session_id,
        to_user=build_session_key(user, session_id),
        body="made it",
    )
    await relay.deliver(message, sender_key=build_session_key(user, session_id))
    fetched = await relay.fetch(build_session_key(user, session_id))
    assert [m.body for m in fetched] == ["made it"]


async def test_a_stored_message_over_a_bound_is_logged_at_error_and_kept(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    relay = LocalRelay(tmp_path)
    good = Message(from_user="kai", to_user="eric", body="fine")
    await relay.deliver(good, sender_key="kai:tty1")
    path = relay._user_inbox_path("eric")
    bad_id = uuid.uuid4()
    bad = good.model_dump_json().replace(str(good.id), str(bad_id))
    bad = bad.replace('"from_user":"kai"', f'"from_user":"{"k" * 500}"')
    path.write_text(path.read_text() + bad + "\n")
    with caplog.at_level(logging.ERROR, logger="biff.relay"):
        fetched = await relay.fetch_user_inbox("eric")
    assert [m.body for m in fetched] == ["fine"]
    assert str(bad_id) in caplog.text
    assert "k" * 50 not in caplog.text  # never the frame body
    assert str(bad_id) in path.read_text()
