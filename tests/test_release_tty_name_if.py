"""A tty name is released only if the releasing session still holds it.

The check and the delete are one compare-and-delete on the reservation, locally
under the relay's lock and in NATS by the revision the check read, so a name
that changed hands after the check is never freed.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.js.errors import BadRequestError, KeyNotFoundError

from biff.nats_relay import NatsRelay
from biff.relay import LocalRelay
from biff.session_id import CLI_ROLE
from biff.session_routing import SessionRouting
from tests.test_daemon.test_session_host import _host

if TYPE_CHECKING:
    from pathlib import Path


async def test_a_local_name_is_released_by_its_owner(tmp_path: Path) -> None:
    relay = LocalRelay(tmp_path)
    await relay.reserve_tty_name("kai", "alpha", "kai:one")
    assert await relay.release_tty_name_if("kai", "alpha", "kai:one") is True
    assert await relay.get_tty_reservation_owner("kai", "alpha") is None


async def test_a_local_name_another_session_holds_is_kept(tmp_path: Path) -> None:
    relay = LocalRelay(tmp_path)
    await relay.reserve_tty_name("kai", "alpha", "kai:two")
    assert await relay.release_tty_name_if("kai", "alpha", "kai:one") is False
    assert await relay.get_tty_reservation_owner("kai", "alpha") == "kai:two"


async def test_a_name_nobody_holds_is_not_released(tmp_path: Path) -> None:
    assert (
        await LocalRelay(tmp_path).release_tty_name_if("kai", "x", "kai:one") is False
    )


class FlippingRelay(LocalRelay):
    """A relay whose name changes hands just before any release."""

    newcomer: ClassVar[str] = ""

    async def _flip(self, user: str, name: str) -> None:
        if FlippingRelay.newcomer:
            key, FlippingRelay.newcomer = FlippingRelay.newcomer, ""
            await LocalRelay.release_tty_name(self, user, name)
            await LocalRelay.reserve_tty_name(self, user, name, key)

    async def release_tty_name(self, user: str, name: str) -> None:
        await self._flip(user, name)
        await LocalRelay.release_tty_name(self, user, name)

    async def release_tty_name_if(self, user: str, name: str, owner: str) -> bool:
        await self._flip(user, name)
        return await LocalRelay.release_tty_name_if(self, user, name, owner)


async def test_a_shell_session_end_never_frees_a_name_that_changed_hands(
    tmp_path: Path,
) -> None:
    host = _host(tmp_path)
    host = dataclasses.replace(host, relay=FlippingRelay(tmp_path / "relay"))
    record = await host.create("alpha")
    assert SessionRouting(record.session_id, role=CLI_ROLE).session_key("kai")
    FlippingRelay.newcomer = "kai:newcomer"
    await host.end(record)
    assert await host.relay.get_tty_reservation_owner("kai", "alpha") == "kai:newcomer"


def _nats(kv: MagicMock) -> NatsRelay:
    relay = NatsRelay(url="tls://fake:4222", repo_name="test")
    relay._ensure_names_kv = AsyncMock(return_value=kv)  # type: ignore[method-assign]
    return relay


def _names_kv(owner: str | None, revision: int = 4) -> MagicMock:
    kv = MagicMock()
    entry = MagicMock()
    entry.value = None if owner is None else owner.encode()
    entry.revision = revision
    kv.get = AsyncMock(return_value=entry)
    kv.delete = AsyncMock()
    return kv


@pytest.mark.anyio()
async def test_a_nats_name_is_deleted_at_the_revision_the_check_read() -> None:
    kv = _names_kv("kai:one", revision=4)
    assert await _nats(kv).release_tty_name_if("kai", "alpha", "kai:one") is True
    kv.delete.assert_awaited_once_with("kai.alpha", last=4)


@pytest.mark.anyio()
async def test_a_nats_name_that_changed_hands_after_the_check_is_kept() -> None:
    kv = _names_kv("kai:one")
    # What nats-py 2.13 really raises from KeyValue.delete at a stale revision:
    # a bad request carrying JetStream's wrong-last-sequence code, not the
    # KeyWrongLastSequenceError that update() translates it to.
    kv.delete = AsyncMock(
        side_effect=BadRequestError(
            code=400, err_code=10071, description="wrong last sequence: 5"
        )
    )
    assert await _nats(kv).release_tty_name_if("kai", "alpha", "kai:one") is False


@pytest.mark.anyio()
async def test_any_other_nats_error_from_the_delete_is_not_swallowed() -> None:
    kv = _names_kv("kai:one")
    kv.delete = AsyncMock(
        side_effect=BadRequestError(code=400, err_code=10003, description="bad")
    )
    with pytest.raises(BadRequestError):
        await _nats(kv).release_tty_name_if("kai", "alpha", "kai:one")


@pytest.mark.anyio()
async def test_a_nats_name_held_by_another_session_is_never_deleted() -> None:
    kv = _names_kv("kai:two")
    assert await _nats(kv).release_tty_name_if("kai", "alpha", "kai:one") is False
    kv.delete.assert_not_awaited()


@pytest.mark.anyio()
async def test_a_nats_name_that_is_gone_is_not_released() -> None:
    kv = _names_kv("kai:one")
    kv.get = AsyncMock(side_effect=KeyNotFoundError)
    assert await _nats(kv).release_tty_name_if("kai", "alpha", "kai:one") is False
