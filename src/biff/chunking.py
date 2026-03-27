"""Message chunking for the ``write`` command.

Splits long messages into parts that fit within the ``Message.body``
character limit (512).  Both the MCP tool and CLI command use this
to auto-split instead of silently truncating.
"""

from __future__ import annotations

MAX_CHUNK_CHARS: int = 512
"""Maximum characters per message chunk.

Matches the ``max_length=512`` constraint on :pyattr:`Message.body`.
"""


def chunk_message(text: str) -> list[str]:
    """Split *text* into chunks of at most :data:`MAX_CHUNK_CHARS` characters.

    Breaks at word boundaries when possible.  If a single word exceeds
    the limit, it is hard-split at the character boundary.

    Returns a list of 1+ non-empty strings whose concatenation (joined
    by spaces) reconstructs the original text.
    """
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]

    words = text.split()
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for word in words:
        # Would adding this word (plus a space separator) exceed the limit?
        addition = len(word) + (1 if current else 0)
        if current and current_len + addition > MAX_CHUNK_CHARS:
            chunks.append(" ".join(current))
            current = []
            current_len = 0

        # Handle single words longer than the limit.
        if len(word) > MAX_CHUNK_CHARS:
            # Flush any accumulated words first.
            if current:
                chunks.append(" ".join(current))
                current = []
                current_len = 0
            # Hard-split the oversized word.
            chunks.extend(
                word[i : i + MAX_CHUNK_CHARS]
                for i in range(0, len(word), MAX_CHUNK_CHARS)
            )
            continue

        current.append(word)
        current_len += addition

    if current:
        chunks.append(" ".join(current))

    return chunks
