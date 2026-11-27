"""The KEY column never shows an invalid npub as if it were a key."""

from __future__ import annotations

import pytest

from biff.models import UserSession
from biff.who_columns import WhoColumns

NPUB = "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6"


def _key_cells(keys: dict[str, str]) -> list[str]:
    sessions = [UserSession(user="jim", tty="a")]
    _, rows = WhoColumns(keys).layout(sessions)
    return rows[0]


def test_a_valid_npub_is_shortened() -> None:
    assert _key_cells({"jim": NPUB}) == ["npub180cvv…jh6w6"]


@pytest.mark.parametrize(
    "bad",
    [
        NPUB[:-1] + "q",
        "npub1nonsense",
        "not-an-npub-at-all-but-long-enough-to-clip",
        "\x1b[31m" + NPUB,
        "\x1b]0;title\x07" + NPUB,
        NPUB + "\x1b[2J",
        "?",
    ],
)
def test_an_invalid_or_failed_lookup_renders_a_question_mark(bad: str) -> None:
    assert _key_cells({"jim": bad}) == ["?"]


def test_an_uppercase_npub_shows_the_canonical_lowercase_abbreviation() -> None:
    assert _key_cells({"jim": NPUB.upper()}) == ["npub180cvv…jh6w6"]


def test_a_mixed_case_npub_renders_a_question_mark() -> None:
    assert _key_cells({"jim": NPUB[:10] + NPUB[10:].upper()}) == ["?"]


def test_a_user_with_no_key_has_a_blank_cell_and_hides_the_column() -> None:
    hidden, _ = WhoColumns({}).layout([UserSession(user="jim", tty="a")])
    assert "NPUB" in hidden
