"""The optional MGR and NPUB columns of the ``/who`` table."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Self, final

from biff._formatting import clip_to_width, terminal_safe
from biff.models import UserSession
from biff.nostr_bech32 import Bech32Key
from biff.session_routing import ID_RE

logger = logging.getLogger(__name__)

# Fixed-column budgets: cells clip by terminal width, and the raw character
# count is bounded first so a label of zero-width code points cannot slip past
# the width clip.
_MAX_LABEL_WIDTH = 40
_MAX_LABEL_CHARS = _MAX_LABEL_WIDTH * 4


@final
class WhoColumns:
    """Cells for the ``/who`` columns that appear only when some row has one.

    MGR names the human behind an agent.  NPUB shows a short npub for each row
    whose user has published one.  It is the key published under that name and
    nothing more: no proof that the session holds it, so it is not an
    authenticity mark.  An empty cell means the row has no value;
    the table drops a column whose every cell is empty.
    """

    __slots__ = ("_keys",)

    _keys: Mapping[str, str]

    def __new__(cls, keys: Mapping[str, str] | None = None) -> Self:
        """Look up each row's npub by user handle in *keys*."""
        self = super().__new__(cls)
        self._keys = keys or {}
        return self

    def layout(
        self, sessions: Sequence[UserSession]
    ) -> tuple[set[str], list[list[str]]]:
        """Return the headers to hide and, per session, the cells that remain."""
        columns = {
            "MGR": [self._mgr_cell(s) for s in sessions],
            "NPUB": [self._key_cell(self._keys.get(s.user, "")) for s in sessions],
        }
        hidden = {header for header, cells in columns.items() if not any(cells)}
        shown = [cells for header, cells in columns.items() if header not in hidden]
        return hidden, [list(row) for row in zip(*shown, strict=True)] or [
            [] for _ in sessions
        ]

    @staticmethod
    def manager(s: UserSession) -> str:
        """Return the biff-addressable human behind an agent row; blank otherwise.

        Derived from the row's own captured data and stored nowhere: the first
        of its ``owner_git`` (``BIFF_OWNER``, else ``git config user.name`` at
        registration, not the effective author, which a provisioned host
        overrides with the bot) and ``launched_by`` (OS user) that is
        addressable and differs from the row's own identity.  A full name such
        as ``Jane Doe`` is not addressable, so it never fills the cell.  Humans
        get none.
        """
        if s.kind != "agent":
            return ""
        own = s.user.strip().casefold()
        for candidate in (s.owner_git, s.launched_by):
            name = candidate.strip()
            if not name:
                continue
            if not ID_RE.fullmatch(name):
                logger.debug("MGR candidate is not an addressable identity")
                continue
            if name.casefold() != own:
                return name
        return ""

    @staticmethod
    def _mgr_cell(s: UserSession) -> str:
        """Render :meth:`manager` as a neutralised, clipped fixed-column cell."""
        text = terminal_safe(WhoColumns.manager(s))[:_MAX_LABEL_CHARS]
        return clip_to_width(text, _MAX_LABEL_WIDTH)

    @staticmethod
    def _key_cell(raw: str) -> str:
        """Clip the row's npub to its first ten and last five characters.

        A value that does not decode as an npub, and a failed lookup, show as
        ``?``: an invalid key is never displayed as if it were one.
        """
        if not raw:
            return ""
        try:
            Bech32Key.PUBLIC.decode(raw)
        except ValueError:
            return "?"
        return Bech32Key.abbreviate(raw)
