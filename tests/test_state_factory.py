"""``StateBlueprint`` picks the relay and owns it by the same test."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.models import BiffConfig
from biff.relay import LocalRelay
from tests._state import create_state

if TYPE_CHECKING:
    from pathlib import Path


class _FalseyRelay(LocalRelay):
    """A relay a truthiness test would mistake for no relay."""

    def __bool__(self) -> bool:
        return False


def test_an_injected_relay_is_used_and_not_owned_even_when_it_is_falsey(
    tmp_path: Path,
) -> None:
    relay = _FalseyRelay(tmp_path / "injected")

    state = create_state(
        BiffConfig(user="kai", repo_name="_test-blueprint"), tmp_path, relay=relay
    )

    assert state.relay is relay
    assert not state.owns_relay


def test_with_no_relay_the_selected_one_is_owned(tmp_path: Path) -> None:
    state = create_state(BiffConfig(user="kai", repo_name="_test-blueprint"), tmp_path)

    assert state.owns_relay
