"""A heartbeat must not overwrite a row another writer changed after it read."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.js.errors import KeyWrongLastSequenceError

from biff.models import UserSession
from biff.nats_relay import NatsRelay

if TYPE_CHECKING:
    from collections.abc import Callable

KEY = "kai:abc123"


class _RevisionedKV:
    """A key-value bucket with revisions: ``update`` refuses a stale revision."""

    def __init__(self, row: UserSession) -> None:
        self._value = row.model_dump_json().encode()
        self._revision = 1
        self.after_get: Callable[[_RevisionedKV], None] | None = None
        self.after_every_get: Callable[[_RevisionedKV], None] | None = None
        self.update_attempts = 0

    def save(self, row: UserSession) -> None:
        """Store *row* as another writer would, bumping the revision."""
        self._value = row.model_dump_json().encode()
        self._revision += 1

    def row(self) -> UserSession:
        return UserSession.model_validate_json(self._value)

    async def get(self, _key: str) -> MagicMock:
        entry = MagicMock()
        entry.value = self._value
        entry.revision = self._revision
        if self.after_get is not None:
            hook, self.after_get = self.after_get, None
            hook(self)
        if self.after_every_get is not None:
            self.after_every_get(self)
        return entry

    async def put(self, _key: str, value: bytes) -> int:
        self._value = value
        self._revision += 1
        return self._revision

    async def update(self, _key: str, value: bytes, last: int) -> int:
        self.update_attempts += 1
        if last != self._revision:
            raise KeyWrongLastSequenceError
        return await self.put(_key, value)


def _relay(kv: _RevisionedKV) -> NatsRelay:
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
async def test_plan_saved_between_read_and_write_survives_a_heartbeat() -> None:
    row = UserSession(user="kai", tty="abc123")
    kv = _RevisionedKV(row)
    kv.after_get = lambda store: store.save(row.model_copy(update={"plan": "ship"}))

    await _relay(kv).heartbeat(KEY)

    assert kv.row().plan == "ship"


@pytest.mark.anyio()
async def test_heartbeat_still_advances_the_idle_clock_after_a_conflict() -> None:
    row = UserSession(user="kai", tty="abc123")
    kv = _RevisionedKV(row)
    kv.after_get = lambda store: store.save(row.model_copy(update={"plan": "ship"}))
    later = datetime.now(UTC) + timedelta(minutes=5)

    await _relay(kv).heartbeat(KEY, later)

    assert kv.row().last_tool_at == later


_TRIES: Final = 3


@pytest.mark.anyio()
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tty_name", "renamed"),
        ("hostname", "other-box"),
        ("pwd", "/elsewhere"),
        ("biff_enabled", False),
    ],
)
async def test_no_field_another_writer_saved_after_the_read_is_overwritten(
    field: str, value: object
) -> None:
    row = UserSession(user="kai", tty="abc123")
    kv = _RevisionedKV(row)
    kv.after_get = lambda store: store.save(row.model_copy(update={field: value}))

    await _relay(kv).heartbeat(KEY)

    assert getattr(kv.row(), field) == value


@pytest.mark.anyio()
async def test_a_heartbeat_that_loses_every_race_gives_up_after_three_tries() -> None:
    row = UserSession(user="kai", tty="abc123")
    kv = _RevisionedKV(row)
    kv.after_every_get = lambda store: store.save(
        store.row().model_copy(update={"plan": f"plan {store.update_attempts}"})
    )

    await _relay(kv).heartbeat(KEY, datetime.now(UTC) + timedelta(minutes=5))

    assert (kv.update_attempts, kv.row().last_active) == (_TRIES, row.last_active)
    assert kv.row().plan.startswith("plan ")


@pytest.mark.anyio()
async def test_the_next_tick_starts_with_a_fresh_budget_after_one_gave_up() -> None:
    row = UserSession(user="kai", tty="abc123")
    kv = _RevisionedKV(row)
    kv.after_every_get = lambda store: store.save(
        store.row().model_copy(update={"plan": "busy"})
    )
    relay = _relay(kv)
    await relay.heartbeat(KEY)
    kv.after_every_get = None
    later = datetime.now(UTC) + timedelta(minutes=5)

    await relay.heartbeat(KEY, later)

    assert (kv.row().last_tool_at, kv.row().plan) == (later, "busy")
