"""One line of a mailbox file: a message, or a loud account of why it is not."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import final

from pydantic import TypeAdapter, ValidationError

from biff.models import Message

__all__ = ["MailboxLine"]

logger = logging.getLogger(__name__)

_FRAME: TypeAdapter[dict[str, object]] = TypeAdapter(dict[str, object])


@final
class MailboxLine:
    """Parses a mailbox line without destroying one it cannot read.

    A line that fails validation stays in the file and is reported at ERROR once,
    naming the message and the fields at fault, never the frame body.
    """

    @staticmethod
    def parse(line: str) -> Message | None:
        """Return the message *line* holds; ``None`` for a blank or invalid line."""
        try:
            return Message.model_validate_json(line) if line.strip() else None
        except (ValidationError, ValueError) as exc:
            MailboxLine._report(
                MailboxLine._identity(line),
                MailboxLine._fields_at_fault(exc),
                line.strip()[:80],  # keeps two unreadable lines apart; never logged
            )
            return None

    @staticmethod
    def _fields_at_fault(exc: ValueError) -> str:
        if not isinstance(exc, ValidationError):
            return "not valid JSON"
        errors = exc.errors(
            include_input=False, include_url=False, include_context=False
        )
        return ", ".join(".".join(map(str, e["loc"])) for e in errors)

    @staticmethod
    def _identity(line: str) -> str:
        """Return the ``id`` *line* names, or ``unknown``."""
        try:
            found = _FRAME.validate_json(line).get("id")
        except ValidationError:
            return "unknown"
        return found if isinstance(found, str) else "unknown"

    @staticmethod
    @lru_cache(maxsize=1024)
    def _report(ident: str, fields: str, _line: str) -> None:
        logger.error(
            "Skipping a mailbox message that fails validation (id %s, %s); "
            "it stays in the mailbox file",
            ident,
            fields,
        )
