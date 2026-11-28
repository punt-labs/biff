"""Every change to a local tty name takes the lock the compare-and-delete holds.

Two relays over one directory stand for two processes: while one holds the lock a
change by the other must wait, or it could swap the reservation between the
compare-and-delete's owner check and its unlink.
"""

from __future__ import annotations

import asyncio
from concurrent import futures
from typing import TYPE_CHECKING

import pytest

from biff.relay import LocalRelay

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from pathlib import Path

type Change = Callable[[LocalRelay], Coroutine[None, None, object]]

CHANGES: dict[str, Change] = {
    "reserve": lambda r: r.reserve_tty_name("kai", "alpha", "kai:one"),
    "release": lambda r: r.release_tty_name("kai", "alpha"),
    "refresh": lambda r: r.refresh_tty_reservation("kai", "alpha", "kai:one"),
    "release_if": lambda r: r.release_tty_name_if("kai", "alpha", "kai:one"),
}


@pytest.mark.parametrize("name", list(CHANGES))
def test_a_change_by_another_relay_waits_for_the_lock(
    tmp_path: Path, name: str
) -> None:
    holder, other = LocalRelay(tmp_path), LocalRelay(tmp_path)
    with futures.ThreadPoolExecutor(max_workers=1) as pool:
        with holder._lock():
            attempt = pool.submit(asyncio.run, CHANGES[name](other))
            with pytest.raises(futures.TimeoutError):
                attempt.result(timeout=0.3)  # still waiting for the lock
        attempt.result(timeout=5)  # released, it goes through
