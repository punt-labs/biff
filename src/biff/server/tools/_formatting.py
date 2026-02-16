"""Shared formatting helpers for tool output."""

from __future__ import annotations

from datetime import UTC, datetime


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
