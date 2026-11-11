"""ServerState binds to exactly one declared session, once."""

from __future__ import annotations

from pathlib import Path

import pytest

from biff.models import BiffConfig
from biff.server.state import SessionAlreadyBoundError, create_state
from biff.session_routing import SessionRouting


def _state(tmp_path: Path):
    return create_state(BiffConfig(user="rmh", repo_name="r"), tmp_path, dormant=True)


def test_new_state_is_unbound(tmp_path: Path) -> None:
    assert _state(tmp_path).is_bound is False


def test_bind_sets_tty_and_session_key(tmp_path: Path) -> None:
    state = _state(tmp_path)
    routing = SessionRouting("sess-1")
    state.bind(routing)
    assert state.is_bound is True
    assert state.tty == routing.routing_id
    assert state.session_key == routing.session_key("rmh")


def test_bind_moves_talk_state_to_the_bound_key(tmp_path: Path) -> None:
    state = _state(tmp_path)
    routing = SessionRouting("sess-1")
    state.bind(routing)
    assert state.talk.partner_key == routing.session_key("rmh")
    assert state.talk.partner_tty == routing.routing_id


def test_binding_the_same_session_again_is_a_no_op(tmp_path: Path) -> None:
    state = _state(tmp_path)
    routing = SessionRouting("sess-1")
    state.bind(routing)
    state.bind(SessionRouting("sess-1"))
    assert state.session_key == routing.session_key("rmh")


def test_binding_a_different_session_is_refused(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.bind(SessionRouting("sess-1"))
    with pytest.raises(SessionAlreadyBoundError, match="sess-2"):
        state.bind(SessionRouting("sess-2"))
    assert state.session_key == SessionRouting("sess-1").session_key("rmh")


def test_bind_derives_the_unread_path_from_the_configured_directory(
    tmp_path: Path,
) -> None:
    state = create_state(
        BiffConfig(user="rmh", repo_name="r"),
        tmp_path,
        dormant=True,
        unread_dir=tmp_path / "unread",
    )
    assert state.unread_path is None
    routing = SessionRouting("sess-1")
    state.bind(routing)
    assert state.unread_path == routing.unread_path(tmp_path / "unread")


def test_bind_leaves_no_unread_path_when_none_was_configured(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.bind(SessionRouting("sess-1"))
    assert state.unread_path is None
