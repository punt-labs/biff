"""Method params are validated and fully consumed."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from biff.daemon.harness_records import HarnessRecords
from biff.daemon.methods import Methods
from biff.daemon.params import Params
from biff.daemon.refusal import RefusalError
from biff.daemon.session_methods import SessionMethods
from biff.daemon.session_store import CliSessionStore
from biff.daemon.sessions import SessionTable
from biff.frame_reply import ErrorCode

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import JsonValue


def _params(**raw: JsonValue) -> Params:
    return Params("m", dict(raw))


def test_text_returns_the_value() -> None:
    assert _params(to="eric").text("to") == "eric"


def test_text_falls_back_to_its_default() -> None:
    assert _params().text("user", "") == ""


def test_a_missing_required_text_names_the_param() -> None:
    with pytest.raises(RefusalError, match="to is required"):
        _params().text("to")


def test_text_rejects_a_number() -> None:
    with pytest.raises(RefusalError, match="to must be a string"):
        _params(to=3).text("to")


def test_integer_rejects_a_boolean() -> None:
    with pytest.raises(RefusalError, match="count must be an integer"):
        _params(count=True).integer("count", 25)


def test_flag_rejects_a_string() -> None:
    with pytest.raises(RefusalError, match="clear must be a boolean"):
        _params(clear="yes").flag("clear", default=False)


def test_done_refuses_a_param_nobody_read() -> None:
    params = _params(message="x", session_key="eric:abc")
    params.text("message")
    with pytest.raises(RefusalError, match="unknown param session_key"):
        params.done()


def test_refusals_carry_the_invalid_params_code() -> None:
    with pytest.raises(RefusalError) as caught:
        _params().text("to")
    assert caught.value.reply_error.code is ErrorCode.INVALID_PARAMS


def test_every_command_object_is_a_method(tmp_path: Path) -> None:
    live = SessionTable(
        heartbeat_interval=1.0,
        org_refresh_interval=1.0,
        records=HarnessRecords(tmp_path / "harness-sessions"),
    )
    methods = Methods(
        SessionMethods(CliSessionStore(tmp_path), live), annotate_status=lambda r: r
    )
    assert set(methods.names) == {
        "who",
        "finger",
        "write",
        "read",
        "plan",
        "last",
        "wall",
        "mesg",
        "tty",
        "status",
        "session_new",
        "session_ls",
        "session_use",
        "session_end",
        "end",
        "arrivals",
        "channel_ack",
        "channel_check",
        "talk",
        "talk_read",
        "talk_end",
    }
