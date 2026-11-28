"""Reading an expired wall must not delete a wall posted after the read."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.js.errors import BadRequestError, KeyNotFoundError

from biff.models import WallPost
from biff.nats_relay import NatsRelay

if TYPE_CHECKING:
    from collections.abc import Callable

_STALE_REVISION = 10071


def _wall(text: str, *, expires_in: timedelta) -> WallPost:
    return WallPost(
        text=text, from_user="kai", expires_at=datetime.now(UTC) + expires_in
    )


class _WallKV:
    """A bucket whose ``delete`` honours ``last`` as JetStream does."""

    def __init__(self, wall: WallPost) -> None:
        self._value: bytes | None = wall.model_dump_json().encode()
        self._revision = 1
        self.after_get: Callable[[_WallKV], None] | None = None

    def post(self, wall: WallPost) -> None:
        """Store *wall* as another writer would, bumping the revision."""
        self._value = wall.model_dump_json().encode()
        self._revision += 1

    def text(self) -> str | None:
        if self._value is None:
            return None
        return WallPost.model_validate_json(self._value).text

    async def get(self, _key: str) -> MagicMock:
        if self._value is None:
            raise KeyNotFoundError
        entry = MagicMock()
        entry.value = self._value
        entry.revision = self._revision
        if self.after_get is not None:
            hook, self.after_get = self.after_get, None
            hook(self)
        return entry

    async def delete(self, _key: str, last: int | None = None) -> bool:
        if last is not None and last != self._revision:
            raise BadRequestError(err_code=_STALE_REVISION)
        self._value = None
        self._revision += 1
        return True


def _relay(kv: _WallKV) -> NatsRelay:
    relay = NatsRelay(url="tls://fake:4222", repo_name="test")
    nc = MagicMock()
    nc.is_closed = False
    nc.is_connected = True
    relay._nc = nc
    relay._health.record_connected(5.0, is_new_connection=True)
    relay._ensure_connected = AsyncMock(  # type: ignore[method-assign]
        return_value=(MagicMock(), kv)
    )
    return relay


@pytest.mark.anyio()
async def test_a_wall_posted_after_the_read_survives_the_expired_delete() -> None:
    kv = _WallKV(_wall("old", expires_in=timedelta(seconds=-5)))
    fresh = _wall("fresh", expires_in=timedelta(hours=1))
    kv.after_get = lambda store: store.post(fresh)

    seen = await _relay(kv).get_wall()

    assert kv.text() == "fresh"
    assert seen is not None
    assert seen.text == "fresh"


@pytest.mark.anyio()
async def test_an_expired_wall_nobody_replaced_is_deleted() -> None:
    kv = _WallKV(_wall("old", expires_in=timedelta(seconds=-5)))

    assert await _relay(kv).get_wall() is None
    assert kv.text() is None
