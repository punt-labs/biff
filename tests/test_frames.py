"""The biffd frames: strict JSON objects that carry a protocol version."""

from __future__ import annotations

import json

import pytest

from biff.frames import (
    PROTOCOL_VERSION,
    ErrorCode,
    Event,
    FrameError,
    Frames,
    Hello,
    Reply,
    ReplyError,
    Request,
    Subscribe,
)


def _hello(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "type": "hello",
        "version": PROTOCOL_VERSION,
        "token": "t",
        "client": "cli",
        "session_id": "s1",
        "main_agent_id": "m1",
        "cwd": "/repo",
    }
    return {**base, **overrides}


def test_hello_round_trips() -> None:
    hello = Hello(
        token="t", client="mcp", session_id="s1", main_agent_id="m1", cwd="/repo"
    )
    assert Frames.decode(Frames.encode(hello)) == hello


def test_request_round_trips_with_params() -> None:
    request = Request(id=3, method="write", params={"to": "eric", "message": "hi"})
    assert Frames.decode(Frames.encode(request)) == request


def test_reply_carries_a_result() -> None:
    reply = Reply(id=1, result={"plan": "x"})
    assert Frames.decode(Frames.encode(reply)) == reply


def test_reply_carries_an_error() -> None:
    reply = Reply(id=1, error=ReplyError(code=ErrorCode.UNKNOWN_METHOD, message="m"))
    assert Frames.decode(Frames.encode(reply)) == reply


def test_subscribe_and_event_round_trip() -> None:
    assert Frames.decode(Frames.encode(Subscribe())) == Subscribe()
    event = Event(seq=7, kind="unread", data={"count": 2})
    assert Frames.decode(Frames.encode(event)) == event


def test_every_frame_carries_the_version() -> None:
    assert json.loads(Frames.encode(Subscribe()))["version"] == PROTOCOL_VERSION


def test_unknown_frame_type_is_refused() -> None:
    with pytest.raises(FrameError, match="frame"):
        Frames.decode(json.dumps({"type": "bogus", "version": 1}))


def test_a_frame_without_a_version_is_refused() -> None:
    raw = _hello()
    del raw["version"]
    with pytest.raises(FrameError, match="version"):
        Frames.decode(json.dumps(raw))


def test_an_unexpected_field_is_refused() -> None:
    with pytest.raises(FrameError, match="extra"):
        Frames.decode(json.dumps(_hello(admin=True)))


def test_non_json_is_refused() -> None:
    with pytest.raises(FrameError, match="JSON"):
        Frames.decode("{not json")


def test_a_non_object_is_refused() -> None:
    with pytest.raises(FrameError, match="object"):
        Frames.decode("[1, 2]")


def test_a_request_id_must_be_positive() -> None:
    raw = {"type": "request", "version": 1, "id": 0, "method": "who"}
    with pytest.raises(FrameError, match="id"):
        Frames.decode(json.dumps(raw))


def test_a_bad_session_id_is_refused_at_the_boundary() -> None:
    with pytest.raises(FrameError, match="session_id"):
        Frames.decode(json.dumps(_hello(session_id="../x")))
