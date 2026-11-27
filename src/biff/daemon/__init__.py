"""biffd: the per-user daemon that owns relay connections and session state.

Nothing is imported here: the executable logs its start before it loads the
relay stack, and any import of a ``biff.daemon`` module runs this file first.
"""

from __future__ import annotations

__all__: list[str] = []
