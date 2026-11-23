"""Claude Code's file-based managed settings, read for the channel policy."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Self, cast, final

from biff.plugins.claude_code.channel_gates import ClosedGate

__all__ = ["ManagedPolicyGate", "ManagedSettings"]

logger = logging.getLogger(__name__)

# The marketplace and plugin name Claude Code knows biff by.
_BIFF = "biff@punt-labs"
_BIFF_ENTRY = '{"marketplace": "punt-labs", "plugin": "biff"}'

_BASE_FILE = "managed-settings.json"
_DROP_IN_DIR = "managed-settings.d"


@final
class ManagedSettings:
    """The channel keys of the managed settings in one system directory.

    Claude Code merges ``managed-settings.json`` first, then each
    ``managed-settings.d/*.json`` in name order: a later scalar replaces an
    earlier one and arrays concatenate.  A file that cannot be read is skipped,
    as if it were absent; a file that is not a JSON object is recorded in
    :attr:`malformed`.
    """

    __slots__ = ("_allowed", "_channels_enabled", "_malformed", "_sources")

    _allowed: frozenset[str]
    _channels_enabled: bool
    _malformed: tuple[Path, ...]
    _sources: tuple[Path, ...]

    def __new__(cls, directory: Path) -> Self:
        """Read and merge the settings files under *directory*."""
        self = super().__new__(cls)
        sources: list[Path] = []
        malformed: list[Path] = []
        enabled: object = None
        allowed: list[object] = []
        for path in cls._candidates(directory):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                malformed.append(path)
                continue
            except OSError:
                logger.debug("managed settings unreadable: %s", path, exc_info=True)
                continue
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                malformed.append(path)
                continue
            if not isinstance(body, dict):
                malformed.append(path)
                continue
            keys = cast("dict[str, object]", body)
            sources.append(path)
            enabled = keys.get("channelsEnabled", enabled)
            listed = keys.get("allowedChannelPlugins")
            if isinstance(listed, list):
                allowed.extend(cast("list[object]", listed))
        self._sources = tuple(sources)
        self._malformed = tuple(malformed)
        self._channels_enabled = enabled is True
        self._allowed = frozenset(cls._entry_name(entry) for entry in allowed)
        return self

    @staticmethod
    def _candidates(directory: Path) -> tuple[Path, ...]:
        """Return the files Claude Code would merge, in merge order."""
        try:
            drop_ins = sorted(
                path
                for path in (directory / _DROP_IN_DIR).glob("*.json")
                if not path.name.startswith(".")
            )
        except OSError:
            drop_ins = []
        return (directory / _BASE_FILE, *drop_ins)

    @staticmethod
    def _entry_name(entry: object) -> str:
        """Return an ``allowedChannelPlugins`` entry as ``plugin@marketplace``."""
        if isinstance(entry, str):
            return entry
        if isinstance(entry, dict):
            fields = cast("dict[str, object]", entry)
            plugin = fields.get("plugin")
            marketplace = fields.get("marketplace")
            if isinstance(plugin, str) and isinstance(marketplace, str):
                return f"{plugin}@{marketplace}"
        return ""

    @property
    def sources(self) -> tuple[Path, ...]:
        """The files that parsed, in merge order."""
        return self._sources

    @property
    def malformed(self) -> tuple[Path, ...]:
        """The files that exist but are not a JSON object."""
        return self._malformed

    @property
    def channels_enabled(self) -> bool:
        """Whether the merged ``channelsEnabled`` is ``true``."""
        return self._channels_enabled

    @property
    def lists_biff(self) -> bool:
        """Whether the merged ``allowedChannelPlugins`` names biff."""
        return _BIFF in self._allowed


@final
class ManagedPolicyGate:
    """Open unless managed settings block channels or leave biff off the list.

    No managed settings, or none that can be read, is no restriction: the
    user's own plan decides.  Server-managed settings and MDM policy are not
    visible to a hook and are not checked.
    """

    __slots__ = ("_directory",)

    _directory: Path | None

    def __new__(cls, directory: Path | None) -> Self:
        """Check the managed settings under *directory*, if there is one."""
        self = super().__new__(cls)
        self._directory = directory
        return self

    @staticmethod
    def directory_for(platform: str) -> Path | None:
        """Return where *platform* keeps managed settings files, if anywhere.

        *platform* is a ``sys.platform`` value.
        """
        if platform == "darwin":
            return Path("/Library/Application Support/ClaudeCode")
        if platform.startswith("linux"):
            return Path("/etc/claude-code")
        return None

    def closed(self) -> tuple[ClosedGate, ...]:
        """Return a closed gate for each policy problem found."""
        if self._directory is None:
            return ()
        settings = ManagedSettings(self._directory)
        problems = [
            ClosedGate(
                reason=f"{path} is not valid JSON",
                fix=f"Ask your administrator to fix or remove {path}.",
            )
            for path in settings.malformed
        ]
        if not settings.sources:
            return tuple(problems)
        where = f"managed settings in {self._directory}"
        if not settings.channels_enabled:
            problems.append(
                ClosedGate(
                    reason=f"{where} do not set channelsEnabled to true",
                    fix="Ask your administrator to set channelsEnabled to true.",
                )
            )
        if not settings.lists_biff:
            problems.append(
                ClosedGate(
                    reason=f"{where} do not list biff in allowedChannelPlugins",
                    fix=(
                        "Ask your administrator to add "
                        f"{_BIFF_ENTRY} to allowedChannelPlugins."
                    ),
                )
            )
        return tuple(problems)
