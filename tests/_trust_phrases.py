"""Words that mark a teammate's message as distrusted or gated on the user.

Every surface a model reads (tool output, tool descriptions, channel events,
server instructions) is checked against this one list, so a reworded label
cannot return on one surface while the others stay clean.
"""

from __future__ import annotations

BANNED_TRUST_PHRASES: tuple[str, ...] = (
    "untrusted",
    "unverified",
    "ask the user",
    "never run",
    "before accepting",
    "claiming to be",
)


def assert_no_trust_phrase(text: str) -> None:
    """Fail when *text* carries any banned trust label or refusal phrase."""
    lowered = text.lower()
    found = [phrase for phrase in BANNED_TRUST_PHRASES if phrase in lowered]
    assert not found, f"trust phrase {found} in: {text!r}"
