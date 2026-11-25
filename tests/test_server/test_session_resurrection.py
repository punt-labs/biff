"""A presence row rebuilt after expiry must not share a tty name with another session.

When a session's row and its tty reservation both expire (NATS TTL), the next
tool call rebuilds the row through ``get_or_create_session``.  Another session
may have claimed the old name meanwhile; the rebuilt row must then take a new
name, and must hold a reservation for whatever name it shows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.models import BiffConfig
from biff.server.tools._descriptions import get_tty_name, set_tty_name
from biff.server.tools._session import get_or_create_session
from biff.tty import claim_tty_name
from tests._state import create_state

if TYPE_CHECKING:
    from pathlib import Path

_REPO = "_test-resurrection"


async def test_a_name_taken_meanwhile_is_not_reused(tmp_path: Path) -> None:
    config = BiffConfig(user="kai", repo_name=_REPO)
    gone = create_state(config, tmp_path, tty="aaaa1111")
    assert await claim_tty_name(gone.relay, "kai", gone.session_key) == "tty1"
    set_tty_name("tty1")
    await get_or_create_session(gone)
    # Both the row and the reservation expire while the laptop sleeps.
    await gone.relay.delete_session(gone.session_key)
    await gone.relay.release_tty_name("kai", "tty1")
    newcomer = create_state(config, tmp_path, tty="bbbb2222")
    assert await claim_tty_name(newcomer.relay, "kai", newcomer.session_key) == "tty1"

    revived = await get_or_create_session(gone)

    assert revived.tty_name == "tty2"
    owners = [
        await gone.relay.get_tty_reservation_owner("kai", name)
        for name in ("tty1", "tty2")
    ]
    assert owners == [newcomer.session_key, gone.session_key]
    # Every holder of the name moves with it, as the tty tool's rename does.
    assert get_tty_name() == "tty2"
    assert gone.talk.my_tty_name == "tty2"
    assert await gone.relay.get_session_tty_hint("kai", gone.tty) == "tty2"


async def test_a_free_name_is_reserved_again(tmp_path: Path) -> None:
    config = BiffConfig(user="kai", repo_name=_REPO)
    gone = create_state(config, tmp_path, tty="aaaa1111")
    assert await claim_tty_name(gone.relay, "kai", gone.session_key) == "tty1"
    set_tty_name("tty1")
    await get_or_create_session(gone)
    await gone.relay.delete_session(gone.session_key)
    await gone.relay.release_tty_name("kai", "tty1")

    revived = await get_or_create_session(gone)

    assert revived.tty_name == "tty1"
    owner = await gone.relay.get_tty_reservation_owner("kai", "tty1")
    assert owner == gone.session_key
