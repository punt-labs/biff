"""Shared formatting helpers for tool output."""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

# Table layout constants -------------------------------------------------------

TABLE_WIDTH = 80
_COL_SEP = "  "
_HEADER_PREFIX = "\u25b6  "
_ROW_PREFIX = "   "
_PREFIX_LEN = 3  # len(_HEADER_PREFIX) == len(_ROW_PREFIX)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# Column specification ---------------------------------------------------------


@dataclass(frozen=True)
class ColumnSpec:
    """Describes one column in a constrained-width table.

    A column with ``fixed=True`` (the default) grows to fit its content
    but never claims more than it needs.  Exactly one column per table
    should have ``fixed=False`` — it receives the remaining width budget
    and its content wraps when it exceeds that budget.
    """

    header: str
    min_width: int
    fixed: bool = True
    align: Literal["left", "right"] = "left"


# Helpers ----------------------------------------------------------------------


def format_idle(dt: datetime) -> str:
    """Format idle time matching BSD ``finger(1)`` / ``w(1)`` style.

    Examples: ``0m``, ``3m``, ``2h``, ``1d``, ``30d``
    """
    now = datetime.now(UTC)
    total_seconds = max(0, int((now - dt).total_seconds()))
    minutes = total_seconds // 60
    hours = minutes // 60
    days = hours // 24

    if days > 0:
        return f"{days}d"
    if hours > 0:
        return f"{hours}h"
    return f"{minutes}m"


def visible_width(s: str) -> int:
    """Return printable width of *s*, ignoring ANSI escape sequences."""
    return len(_ANSI_RE.sub("", s))


def last_component(path: str) -> str:
    """Return the last component of a path, or the original for short values.

    ``/Users/kai/projects/biff`` → ``biff``
    ``/biff/`` → ``biff``
    ``-`` → ``-``
    """
    stripped = path.rstrip("/")
    if not stripped or stripped == "-":
        return path
    return stripped.rsplit("/", 1)[-1]


# Table formatter --------------------------------------------------------------


def _fmt_cell(text: str, width: int, align: Literal["left", "right"]) -> str:
    """Pad *text* to *width* using visible width (ANSI-aware)."""
    padding = max(0, width - visible_width(text))
    if align == "right":
        return " " * padding + text
    return text + " " * padding


def _render_rows(
    specs: list[ColumnSpec],
    rows: list[list[str]],
    col_widths: list[int],
    var_idx: int | None,
    var_offset: int,
) -> list[str]:
    """Render data rows, wrapping the variable column when needed."""
    n = len(specs)
    output: list[str] = []
    indent = " " * var_offset

    for row in rows:
        if var_idx is None:
            cells = [_fmt_cell(row[i], col_widths[i], specs[i].align) for i in range(n)]
            output.append(_ROW_PREFIX + _COL_SEP.join(cells))
        else:
            chunks = textwrap.wrap(row[var_idx], col_widths[var_idx]) or [""]
            for chunk_i, chunk in enumerate(chunks):
                if chunk_i == 0:
                    cells = [
                        _fmt_cell(
                            chunk if i == var_idx else row[i],
                            col_widths[i],
                            specs[i].align,
                        )
                        for i in range(n)
                    ]
                    output.append(_ROW_PREFIX + _COL_SEP.join(cells))
                else:
                    output.append(indent + chunk)

    return output


def format_table(specs: list[ColumnSpec], rows: list[list[str]]) -> str:
    """Render a constrained-width table with header and data rows.

    The table fits within :data:`TABLE_WIDTH` (80) columns.  Fixed
    columns grow to their content width.  The single variable column
    (``fixed=False``) receives the remaining width budget.  Variable
    column content that exceeds its allocation wraps onto continuation
    lines, indented to the variable column's start position.

    Returns ``▶  HEADER\\n   row\\n   row...`` format matching BSD
    ``w(1)`` style.
    """
    n = len(specs)

    # Identify the variable column (at most one).
    var_idx: int | None = None
    for i, spec in enumerate(specs):
        if not spec.fixed:
            if var_idx is not None:
                msg = "format_table: at most one variable column allowed"
                raise ValueError(msg)
            var_idx = i

    # Step 1: measure content widths for every column.
    col_widths: list[int] = []
    for i, spec in enumerate(specs):
        content_max = max(
            (visible_width(row[i]) for row in rows),
            default=0,
        )
        col_widths.append(max(spec.min_width, len(spec.header), content_max))

    # Step 2: constrain the variable column to the remaining budget.
    sep_total = len(_COL_SEP) * (n - 1)
    if var_idx is not None:
        fixed_total = sum(w for i, w in enumerate(col_widths) if i != var_idx)
        budget = TABLE_WIDTH - _PREFIX_LEN - fixed_total - sep_total
        budget = max(specs[var_idx].min_width, budget)
        col_widths[var_idx] = budget

    # Step 3: variable column start offset for continuation indentation.
    if var_idx is not None:
        var_offset = _PREFIX_LEN + sum(col_widths[:var_idx]) + len(_COL_SEP) * var_idx
    else:
        var_offset = 0

    # Step 4: render header.
    header_cells = [
        _fmt_cell(spec.header, col_widths[i], spec.align)
        for i, spec in enumerate(specs)
    ]
    header = _HEADER_PREFIX + _COL_SEP.join(header_cells)

    # Step 5: render rows with wrapping on the variable column.
    body = _render_rows(specs, rows, col_widths, var_idx, var_offset)
    return "\n".join([header, *body])
