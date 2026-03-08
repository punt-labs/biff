"""Vox integration — optional voice synthesis for biff events.

Biff discovers vox at L0 (sentinel) and L1 (binary). When present,
biff can fire-and-forget voice synthesis for high-signal events like
wall broadcasts. When absent, biff works identically — text only.

Vox has no knowledge of biff. The dependency arrow is unidirectional:
biff → vox (consumer → building block).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_UNCHECKED = object()
_vox_binary: str | None | object = _UNCHECKED


def has_vox(repo_root: Path | None = None) -> bool:
    """L0: Check whether vox is configured in this repo."""
    root = repo_root or Path.cwd()
    return (root / ".vox" / "config.md").is_file()


def vox_binary() -> str | None:
    """L1: Discover vox binary on PATH. Lazy, cached for session."""
    global _vox_binary
    if _vox_binary is _UNCHECKED:
        _vox_binary = shutil.which("vox")
        if _vox_binary is None:
            logger.debug("vox binary not found on PATH")
    if _vox_binary is _UNCHECKED:
        return None  # unreachable, satisfies type checker
    return _vox_binary  # type: ignore[return-value]


# ── Emoticon-to-vibe mapping ───────────────────────────────────────

_EMOTICON_VIBES: tuple[tuple[str, str], ...] = (
    (";-)", "[playful] [warm]"),
    (";)", "[playful] [warm]"),
    (":D", "[excited] [cheerful]"),
    (":-D", "[excited] [cheerful]"),
    (":)", "[warm] [friendly]"),
    (":-)", "[warm] [friendly]"),
    (":(", "[sad] [empathetic]"),
    (":-(", "[sad] [empathetic]"),
    (">:(", "[frustrated] [intense]"),
    (":P", "[playful] [cheeky]"),
    (":-P", "[playful] [cheeky]"),
    ("<3", "[warm] [affectionate]"),
    ("!!", "[urgent] [intense]"),
    ("??", "[curious] [confused]"),
)

WALL_DEFAULT_VIBES = "[alert] [serious]"


def vibes_from_text(text: str) -> str:
    """Extract vibe tags from emoticons in *text*.

    Scans for text emoticons (e.g. ``;-)``, ``:D``, ``:(`` ) and
    returns the vibe tags for the first match. Returns
    :data:`WALL_DEFAULT_VIBES` when no emoticon matches.
    """
    for emoticon, vibes in _EMOTICON_VIBES:
        if emoticon in text:
            return vibes
    return WALL_DEFAULT_VIBES


# ── Background task bookkeeping ───────────────────────────────────

# Background tasks must be stored to prevent GC from cancelling them.
_background_tasks: set[asyncio.Task[None]] = set()


def speak_fire_and_forget(
    text: str,
    *,
    vibe_tags: str = "",
) -> None:
    """Synthesize speech via vox subprocess, non-blocking.

    Spawns ``vox unmute`` as a detached subprocess using
    ``asyncio.create_subprocess_exec`` (no shell interpolation).
    Does not wait for completion, does not capture output. If vox
    is absent or the subprocess fails, the error is logged and
    swallowed.

    Vibe tags are appended inline to the text (e.g. ``"hello
    [friendly]"``). ElevenLabs resolves inline tags as expressive
    cues without needing a separate ``vox vibe`` call.
    """
    binary = vox_binary()
    if binary is None:
        return

    utterance = f"{text} {vibe_tags}".strip() if vibe_tags else text
    args = [binary, "unmute", utterance]

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("No running event loop; skipping vox speak")
        return

    async def _spawn() -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            # Don't await proc.wait() — true fire-and-forget.
            # The process runs independently; OS reaps it.
            logger.debug("vox speak spawned (pid=%s)", proc.pid)
        except OSError:
            logger.debug("Failed to spawn vox speak", exc_info=True)

    task = loop.create_task(_spawn())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
