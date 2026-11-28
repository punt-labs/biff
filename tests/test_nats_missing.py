"""A purged stream is not an empty inbox: it is warned about once and fails the read."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.js.errors import BucketNotFoundError, NotFoundError

from biff.nats_missing import StoreMissingError
from biff.nats_relay import NatsRelay

if TYPE_CHECKING:
    from collections.abc import Awaitable

_STREAM_NOT_FOUND = 10059
_MESSAGE_NOT_FOUND = 10037


def _relay_and_js(
    monkeypatch: pytest.MonkeyPatch,
    *,
    last_msg: Exception | None = None,
    stream_info: Exception | None = None,
) -> tuple[NatsRelay, MagicMock]:
    relay = NatsRelay(url="tls://fake:4222", repo_name="test")
    js = MagicMock()
    js.get_last_msg = AsyncMock(side_effect=last_msg)
    js.stream_info = AsyncMock(side_effect=stream_info)
    kv = MagicMock()
    kv.get = AsyncMock(side_effect=BucketNotFoundError())

    async def tracked(_what: str, awaitable: Awaitable[object]) -> object:
        return await awaitable

    monkeypatch.setattr(relay, "_ensure_connected", AsyncMock(return_value=(js, kv)))
    monkeypatch.setattr(relay, "_tracked", tracked)
    return relay, js


def _not_found(code: int) -> NotFoundError:
    return NotFoundError(code=404, err_code=code, description="not found")


async def test_a_missing_stream_fails_the_high_water_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relay, _ = _relay_and_js(monkeypatch, last_msg=_not_found(_STREAM_NOT_FOUND))
    with pytest.raises(StoreMissingError, match="is missing"):
        await relay.inbox_high_water("kai:t")


async def test_a_missing_stream_fails_the_unread_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relay, _ = _relay_and_js(monkeypatch, stream_info=_not_found(_STREAM_NOT_FOUND))
    with pytest.raises(StoreMissingError, match="is missing"):
        await relay.get_unread_summary("kai:t")


async def test_an_empty_inbox_is_still_zero_and_silent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    relay, _ = _relay_and_js(monkeypatch, last_msg=_not_found(_MESSAGE_NOT_FOUND))
    with caplog.at_level(logging.WARNING):
        assert await relay.inbox_high_water("kai:t") == 0
    assert caplog.records == []


async def test_a_missing_stream_is_warned_once_per_episode(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    relay, _ = _relay_and_js(monkeypatch, last_msg=_not_found(_STREAM_NOT_FOUND))
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            with pytest.raises(StoreMissingError):
                await relay.inbox_high_water("kai:t")
    assert len([r for r in caplog.records if "is missing" in r.getMessage()]) == 1


async def test_a_stream_that_comes_back_ends_the_episode(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    gone = _not_found(_STREAM_NOT_FOUND)
    relay, js = _relay_and_js(monkeypatch, last_msg=gone)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(StoreMissingError):
            await relay.inbox_high_water("kai:t")
        js.get_last_msg.side_effect = None
        js.get_last_msg.return_value = MagicMock(seq=7)
        assert await relay.inbox_high_water("kai:t") == 7
        js.get_last_msg.side_effect = gone
        with pytest.raises(StoreMissingError):
            await relay.inbox_high_water("kai:t")
    assert len([r for r in caplog.records if "is missing" in r.getMessage()]) == 2


async def test_a_missing_wall_bucket_reads_as_no_wall_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    relay, _ = _relay_and_js(monkeypatch)
    with caplog.at_level(logging.WARNING):
        assert await relay.get_wall() is None
        assert await relay.get_wall() is None
    assert len([r for r in caplog.records if "is missing" in r.getMessage()]) == 1
