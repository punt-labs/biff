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
from contextlib import suppress
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

# Wall broadcasts fan out to N Claude Code sessions in the same repo and
# each session spawns ``vox unmute`` with identical text. Without dedup,
# the user hears the same sentence N times. ``vox unmute --once <seconds>``
# (added in punt-vox PR #171) asks voxd to skip the play if the same text
# was spoken within the window. 600 s (10 min) is short enough that a
# deliberately *repeated* wall (same text, re-posted later) plays again,
# and long enough to absorb the full fan-out plus any stragglers from a
# session that reconnects mid-broadcast. The default wall TTL is 1 h, so
# the dedup window is strictly shorter — dedup cannot suppress a later
# repost even while the original wall is still active.
WALL_DEDUP_SECONDS = 600


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

# Track spawned processes so we can kill+wait during shutdown.
_spawned_procs: set[asyncio.subprocess.Process] = set()


async def drain_background_tasks() -> None:
    """Kill spawned vox processes and await their background tasks.

    Called during server shutdown to ensure asyncio's child watcher
    deregisters all PIDs before the event loop closes.  Without this,
    SIGCHLD fires ``_do_waitpid`` on a closed loop, producing logging
    errors on Python 3.13+.
    """
    for proc in list(_spawned_procs):
        with suppress(OSError):
            proc.kill()
    # Await remaining tasks — they will complete quickly after kill.
    tasks = list(_background_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _background_tasks.clear()
    _spawned_procs.clear()


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
    # --once <seconds> deduplicates fan-out spam. The only caller is the
    # wall refresh path in ``_descriptions.py`` (talk/write skip vox), so
    # the gate is structural — no need to branch on call site.
    args = [binary, "unmute", "--once", str(WALL_DEDUP_SECONDS), utterance]

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
            logger.debug("vox speak spawned (pid=%s)", proc.pid)
            _spawned_procs.add(proc)
            # Must await proc.wait() so asyncio's child watcher deregisters
            # the PID before the event loop closes.  Without this, SIGCHLD
            # fires _do_waitpid on a closed loop, causing logging errors.
            await proc.wait()
            _spawned_procs.discard(proc)
        except OSError:
            logger.debug("Failed to spawn vox speak", exc_info=True)

    task = loop.create_task(_spawn())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
