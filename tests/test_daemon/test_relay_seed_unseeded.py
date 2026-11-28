"""A biffd started without relay settings refuses a client that carries them."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from biff.client.connection import DaemonError

if TYPE_CHECKING:
    from tests.test_daemon.conftest import Harness


async def test_a_client_with_relay_settings_is_told_biffd_was_started_without_them(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIFF_RELAY_TOKEN", "tok-B")
    with pytest.raises(DaemonError, match="started without BIFF_RELAY_TOKEN") as err:
        await harness.connect("s-eric", "eric")
    assert "tok-B" not in str(err.value)


async def test_a_client_without_relay_settings_is_accepted(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai")
    assert await kai.request("plan", message="plain") is not None
