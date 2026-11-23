"""Read the managed-settings sources a hook can see on this machine."""

from __future__ import annotations

import json
import logging
import plistlib
from pathlib import Path
from typing import Final, cast, final
from xml.parsers.expat import ExpatError

__all__ = ["ManagedSources"]

logger = logging.getLogger(__name__)

type Source = dict[str, object]

_BASE_FILE = "managed-settings.json"
_DROP_IN_DIR = "managed-settings.d"


@final
class ManagedSources:
    """Parsed managed settings, highest priority first.

    Claude Code ranks managed sources: server-managed settings, then OS-level
    policy (the macOS preference domain, the Windows registry), then the
    settings files, then the Windows user registry.  Server-managed settings
    and the Windows registry are not visible to a hook and are not read.  A
    source that is absent, unreadable or malformed is skipped; Claude Code
    itself refuses to start on a malformed managed JSON file, so that case
    never reaches a session.
    """

    PLIST_MACOS: Final = Path(
        "/Library/Managed Preferences/com.anthropic.claudecode.plist"
    )
    """The macOS MDM preference domain ``com.anthropic.claudecode``."""

    @staticmethod
    def for_platform(platform: str) -> tuple[Source, ...]:
        """Return the sources on *platform* (a ``sys.platform`` value), best first."""
        found: list[Source | None] = []
        if platform == "darwin":
            found.append(ManagedSources.plist(ManagedSources.PLIST_MACOS))
        directory = ManagedSources.directory_for(platform)
        if directory is not None:
            found.append(ManagedSources.directory(directory))
        return tuple(source for source in found if source is not None)

    @staticmethod
    def directory_for(platform: str) -> Path | None:
        """Return where *platform* keeps managed settings files, if anywhere."""
        if platform == "darwin":
            return Path("/Library/Application Support/ClaudeCode")
        if platform.startswith("linux"):
            return Path("/etc/claude-code")
        if platform == "win32":
            return Path(r"C:\Program Files\ClaudeCode")
        return None

    @staticmethod
    def plist(path: Path) -> Source | None:
        """Return the preference domain at *path*, or ``None`` when unreadable."""
        try:
            with path.open("rb") as handle:
                body = plistlib.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, plistlib.InvalidFileException, ExpatError, ValueError):
            logger.info("managed settings unreadable: %s", path, exc_info=True)
            return None
        if not isinstance(body, dict):
            logger.info("managed settings are not a dictionary: %s", path)
            return None
        return cast("Source", body)

    @staticmethod
    def directory(directory: Path) -> Source | None:
        """Return ``managed-settings.json`` merged with its drop-ins.

        The base file merges first, then each ``managed-settings.d/*.json`` in
        name order; hidden files are ignored.  ``None`` when no file read.
        """
        merged: Source | None = None
        drop_ins = ManagedSources._drop_ins(directory / _DROP_IN_DIR)
        for path in (directory / _BASE_FILE, *drop_ins):
            body = ManagedSources._json(path)
            if body is not None:
                merged = body if merged is None else ManagedSources.merged(merged, body)
        return merged

    @staticmethod
    def _drop_ins(directory: Path) -> list[Path]:
        """Return the visible ``*.json`` files in *directory*, in name order."""
        try:
            names = list(directory.iterdir())
        except FileNotFoundError:
            return []
        except OSError:
            logger.info("managed settings unreadable: %s", directory, exc_info=True)
            return []
        return sorted(
            path
            for path in names
            if path.suffix == ".json" and not path.name.startswith(".")
        )

    @staticmethod
    def merged(low: Source, high: Source) -> Source:
        """Return *high* over *low*: scalars replace, arrays concatenate."""
        result = dict(low)
        for key, value in high.items():
            earlier = result.get(key)
            if isinstance(value, list) and isinstance(earlier, list):
                result[key] = [
                    *cast("list[object]", earlier),
                    *cast("list[object]", value),
                ]
            else:
                result[key] = value
        return result

    @staticmethod
    def _json(path: Path) -> Source | None:
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.info("managed settings unreadable: %s", path, exc_info=True)
            return None
        if not isinstance(body, dict):
            logger.info("managed settings are not a JSON object: %s", path)
            return None
        return cast("Source", body)
