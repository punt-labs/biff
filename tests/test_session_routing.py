"""SessionRouting is the one derivation of routing id, key and unread path."""

from __future__ import annotations

from pathlib import Path

import pytest

from biff.caller_identity import CallerIdentity
from biff.session_id import CLAUDE_CODE_ROLE, derive_routing_id
from biff.session_routing import SessionIdError, SessionRouting


def test_routing_id_uses_the_claude_code_role() -> None:
    routing = SessionRouting("sess-1")
    assert routing.routing_id == derive_routing_id("sess-1", CLAUDE_CODE_ROLE)


def test_session_key_is_user_and_routing_id() -> None:
    routing = SessionRouting("sess-1")
    assert routing.session_key("rmh") == f"rmh:{routing.routing_id}"


def test_unread_path_is_keyed_by_session_id(tmp_path: Path) -> None:
    # The shell nudge hook cannot compute a blake2b digest, so the unread file
    # is named by the (already validated) session_id it reads from its payload.
    assert SessionRouting("sess-1").unread_path(tmp_path) == tmp_path / "sess-1.json"


def test_hook_server_and_statusline_agree_on_every_key(tmp_path: Path) -> None:
    sid = "7f3c9a-4b2e"
    from_hook = SessionRouting(sid)  # SessionStart payload session_id
    from_server = SessionRouting(CallerIdentity(sid, "agent-9").session_id)
    from_statusline = SessionRouting(sid)  # statusline stdin session_id
    assert (
        from_hook.session_key("u")
        == from_server.session_key("u")
        == from_statusline.session_key("u")
    )
    assert (
        from_hook.unread_path(tmp_path)
        == from_server.unread_path(tmp_path)
        == from_statusline.unread_path(tmp_path)
    )


def test_caller_identity_exposes_its_routing() -> None:
    caller = CallerIdentity("sess-1", "a")
    assert caller.routing == SessionRouting("sess-1")


@pytest.mark.parametrize("bad", ["", "../x", "a b", "a\n"])
def test_malformed_session_id_is_rejected(bad: str) -> None:
    with pytest.raises(SessionIdError, match="session_id"):
        SessionRouting(bad)


def test_statusline_reads_the_file_the_server_writes(tmp_path: Path) -> None:
    import io
    import json
    from unittest.mock import patch

    from biff.statusline import run_statusline

    sid = "7f3c9a-4b2e"
    unread_dir = tmp_path / "unread"
    unread_dir.mkdir()
    server_path = CallerIdentity(sid, "agent-9").routing.unread_path(unread_dir)
    server_path.write_text(
        json.dumps({"user": "kai", "count": 4, "tty_name": "tty1", "display_items": []})
    )
    with patch(
        "biff.statusline.sys.stdin", io.StringIO(json.dumps({"session_id": sid}))
    ):
        result = run_statusline(tmp_path / "stash.json", unread_dir)
    assert "kai:tty1(4)" in result
