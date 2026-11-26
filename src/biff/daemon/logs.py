"""Where biffd writes its log, and the hint that names it."""

from __future__ import annotations

from typing import Final

__all__ = ["LOG_HINT", "LOG_PATH"]

LOG_PATH: Final = "~/.punt-labs/biff/logs/biff.log"
"""The file biff's diagnostics go to; ``biff daemon serve`` writes it too."""

LOG_HINT: Final = f"see {LOG_PATH}"
