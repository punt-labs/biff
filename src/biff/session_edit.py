"""A compare-and-set edit of one presence row."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from biff.models import UserSession

type RowChange = Callable[[UserSession], UserSession]

__all__ = ["RowChange", "SessionChangedError", "SessionEdit"]


class SessionChangedError(ConnectionError):
    """A session row changed under every retry of a compare-and-set."""


@dataclass(frozen=True)
class SessionEdit:
    """What to do to the row *key*, decided from the row as it is read.

    ``change`` gets the row and returns the row to store; returning its
    argument unchanged stores nothing.  A relay may call it more than once, once
    per attempt, so it must decide afresh each time and have no side effects but
    recording what it decided.
    """

    key: str
    change: RowChange
