"""Malformed-frame handling in ``NatsRelay._fetch_from_subject`` (biff-cuy).

A frame that fails ``Message`` validation must never be acked: acking
removes it from the WORK_QUEUE as if delivered, silently destroying the
evidence of a wire-integrity fault.  The fetch loop instead ``term()``s
the malformed frame — JetStream's poison-message signal, which stops
redelivery (no nak DoS loop) and emits a ``MSG_TERMINATED`` advisory so
the drop is observable off-box — and logs at ERROR.  A valid frame still
acks normally (WORK_QUEUE deletes it on ack).

These tests mock JetStream at the boundary, so they run in tiers 1-2 (no
``nats`` marker, no real server).
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from biff.models import Message
from biff.nats_relay import NatsRelay

_LOGGER_NAME = "biff.nats_relay"
_SUBJECT = "biff.test.inbox.eric.tty2"

# Short enough to survive pydantic's input-value truncation in a
# ``string_too_long`` error, so any leak of the frame body shows in full.
_SENTINEL = "LEAK_SEKRIT_body"


def _raw(data: bytes) -> MagicMock:
    """A JetStream ``Msg`` stand-in recording ack/term calls."""
    raw = MagicMock()
    raw.data = data
    raw.ack = AsyncMock()
    raw.term = AsyncMock()
    return raw


def _valid_payload(body: str) -> bytes:
    msg = Message(from_user="kai", to_user="eric:tty2", body=body)
    return msg.model_dump_json().encode()


def _relay_with(raw_msgs: list[MagicMock]) -> tuple[NatsRelay, MagicMock]:
    """A relay whose next fetch returns *raw_msgs*, mocked at the JS boundary."""
    relay = NatsRelay(url="tls://fake:4222", repo_name="test")
    nc = MagicMock()
    nc.is_closed = False
    nc.flush = AsyncMock()
    relay._nc = nc
    sub = MagicMock()
    sub.fetch = AsyncMock(return_value=raw_msgs)
    sub.unsubscribe = AsyncMock()
    js = MagicMock()
    js.pull_subscribe = AsyncMock(return_value=sub)
    js.delete_consumer = AsyncMock()
    return relay, js


class TestMalformedFrameNotSilentlyDropped:
    async def test_malformed_frame_is_termed_not_acked(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bad = _raw(b"{ not valid json")
        relay, js = _relay_with([bad])
        caplog.set_level(logging.ERROR, logger=_LOGGER_NAME)

        messages = await relay._fetch_from_subject(js, subject=_SUBJECT, durable="d")

        assert messages == []
        bad.ack.assert_not_awaited()  # never acked-and-deleted as if delivered
        bad.term.assert_awaited_once()  # dead-lettered via JetStream term
        assert any(
            r.levelno == logging.ERROR and "malformed" in r.getMessage().lower()
            for r in caplog.records
        ), "the drop must surface loudly at ERROR, not a silent warning"

    async def test_valid_frame_still_acks(self) -> None:
        good = _raw(_valid_payload("ok"))
        relay, js = _relay_with([good])

        messages = await relay._fetch_from_subject(js, subject=_SUBJECT, durable="d")

        assert [m.body for m in messages] == ["ok"]
        good.ack.assert_awaited_once()  # WORK_QUEUE deletes on ack
        good.term.assert_not_awaited()

    async def test_mixed_batch_terms_bad_acks_good(self) -> None:
        good = _raw(_valid_payload("keep"))
        bad = _raw(b"\xff\xfe not json")
        relay, js = _relay_with([good, bad])

        messages = await relay._fetch_from_subject(js, subject=_SUBJECT, durable="d")

        assert [m.body for m in messages] == ["keep"]
        good.ack.assert_awaited_once()
        good.term.assert_not_awaited()
        bad.ack.assert_not_awaited()
        bad.term.assert_awaited_once()


def _too_long_body_frame() -> bytes:
    """A valid-JSON frame whose body starts with the sentinel and exceeds 512."""
    body = _SENTINEL + "x" * 600  # > 512 → string_too_long ValidationError
    payload = '{"from_user":"kai","to_user":"eric:tty2","body":"' + body + '"}'
    return payload.encode()


def _json_invalid_frame() -> bytes:
    """A malformed-JSON frame carrying the sentinel in its raw bytes."""
    return ('{"body": "' + _SENTINEL + '" not json').encode()


class TestMalformedFrameLogDoesNotLeakContent:
    """The ERROR log for a dropped frame must never carry frame content.

    ``ValidationError.__str__`` embeds ``input_value`` — the raw frame body — so
    interpolating the exception would leak private message plaintext.  Because
    ``term()`` deletes the frame, that log line would be the *only* surviving
    copy of the leaked content.  The scrub logs only field locations and error
    types.
    """

    @pytest.mark.parametrize(
        "payload",
        [_too_long_body_frame(), _json_invalid_frame()],
        ids=["too_long_body", "json_invalid"],
    )
    async def test_error_log_never_carries_frame_content(
        self, payload: bytes, caplog: pytest.LogCaptureFixture
    ) -> None:
        bad = _raw(payload)
        relay, js = _relay_with([bad])
        caplog.set_level(logging.ERROR, logger=_LOGGER_NAME)

        messages = await relay._fetch_from_subject(js, subject=_SUBJECT, durable="d")

        assert messages == []
        bad.term.assert_awaited_once()  # still poison-terminated
        bad.ack.assert_not_awaited()
        for record in caplog.records:
            assert _SENTINEL not in record.getMessage()
