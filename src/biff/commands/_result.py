"""Command result type for the library API.

Every command function returns a ``CommandResult``. The CLI adapter
interprets it (JSON vs human text, exit codes); library callers
inspect the fields directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CommandResult:
    """Outcome of a command invocation.

    Attributes:
        text: Human-readable output.
        json_data: JSON-serializable payload; ``None`` means use *text*.
        error: ``True`` signals exit code 1 in CLI, inspectable in library.
    """

    text: str
    json_data: object = field(default=None)
    error: bool = False
