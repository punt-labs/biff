"""The biffd frames: strict JSON objects that carry a protocol version."""

from __future__ import annotations

import json

import pytest

from biff.frame_base import PROTOCOL_VERSION
from biff.frame_client import Hello, Request, Subscribe
from biff.frame_proof import Challenge, Handshake, Proof
from biff.frame_reply import ErrorCode, Reply, ReplyError
from biff.frames import Event, FrameError, Frames


def _hello(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "type": "hello",
        "version": PROTOCOL_VERSION,
        "mac": "ab" * 32,
        "client": "cli",
        "session_id": "s1",
        "main_agent_id": "m1",
        "cwd": "/repo",
    }
    return {**base, **overrides}


def test_hello_round_trips() -> None:
    hello = Hello(
        mac="ab" * 32, client="mcp", session_id="s1", main_agent_id="m1", cwd="/repo"
    )
    assert Frames.decode(Frames.encode(hello)) == hello


def test_an_ordinary_hello_omits_ends_so_an_older_daemon_still_reads_it() -> None:
    hello = Hello(mac="ab" * 32, client="mcp", session_id="s1", cwd="/repo")

    assert "ends" not in json.loads(Frames.encode(hello))


def test_an_ending_hello_carries_ends_and_round_trips() -> None:
    hello = Hello(mac="ab" * 32, client="hook", session_id="s1", cwd="/repo", ends=True)

    assert json.loads(Frames.encode(hello))["ends"] is True
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
    assert Frames.decode(Frames.encode(Subscribe(id=1))) == Subscribe(id=1)
    event = Event(seq=7, kind="unread", data={"count": 2})
    assert Frames.decode(Frames.encode(event)) == event


def test_every_frame_carries_the_version() -> None:
    assert json.loads(Frames.encode(Subscribe(id=1)))["version"] == PROTOCOL_VERSION


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


def test_challenge_and_proof_round_trip() -> None:
    challenge = Challenge.fresh()
    assert Frames.decode(Frames.encode(challenge)) == challenge
    proof = Proof(daemon_nonce=Handshake.fresh_nonce(), mac="cd" * 32)
    assert Frames.decode(Frames.encode(proof)) == proof


def test_a_proof_is_valid_only_for_its_own_challenge_and_token() -> None:
    challenge = Challenge.fresh()
    nonce = Handshake.fresh_nonce()
    proof = Proof(
        daemon_nonce=nonce,
        mac=Handshake.daemon_mac("token", challenge.client_nonce, nonce),
    )
    assert proof.valid_for(challenge, "token")
    assert not proof.valid_for(challenge, "other-token")
    assert not proof.valid_for(Challenge.fresh(), "token")


def test_fresh_nonces_are_128_bits_and_differ() -> None:
    one, two = Handshake.fresh_nonce(), Handshake.fresh_nonce()
    assert (len(one), one != two) == (32, True)


@pytest.mark.parametrize("nonce", ["zz" * 16, "ab"])
def test_a_non_hex_or_short_nonce_is_refused(nonce: str) -> None:
    raw = {"type": "challenge", "version": 1, "client_nonce": nonce}
    with pytest.raises(FrameError, match="client_nonce"):
        Frames.decode(json.dumps(raw))


def test_a_hello_has_no_token_field() -> None:
    raw = _hello(token="t")
    with pytest.raises(FrameError, match="token"):
        Frames.decode(json.dumps(raw))


def test_version_of_reads_the_version_without_validating_the_frame() -> None:
    raw = json.dumps({"type": "hello", "version": 7, "anything": [1, 2]})
    assert Frames.version_of(raw) == 7


@pytest.mark.parametrize(
    "text", ["{bad", "[1]", json.dumps({"type": "hello"}), json.dumps({"version": "1"})]
)
def test_version_of_is_none_when_there_is_no_integer_version(text: str) -> None:
    assert Frames.version_of(text) is None


def test_a_subscribe_carries_an_id_to_answer() -> None:
    assert Frames.decode(Frames.encode(Subscribe(id=4))) == Subscribe(id=4)


@pytest.mark.parametrize("kind", ["unread", "mail", "wall", "degraded", "recovered"])
def test_every_event_kind_round_trips(kind: str) -> None:
    event = Event.model_validate({"seq": 1, "kind": kind})
    assert Frames.decode(Frames.encode(event)) == event
