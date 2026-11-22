"""The plugin contract: what every AI tool's adapter implements (DES-088).

Biff is the same for every tool.  A tool differs in three ways, one
interface each: what must be installed for it (:class:`Installer`), how an
arrival reaches its model (:class:`DeliverySink`), and what a session sees
at start (:class:`SessionStart`).  :class:`PluginRegistry` maps a tool name
to its :class:`ToolAdapter`.  ``cli`` fills :data:`REGISTRY` at startup;
nothing below ``cli`` imports an adapter.

The signatures are derived from the Claude Code code as it stands:
``RepoEnablement`` / ``ProjectPermissions`` / ``ClaudeMdImport`` (install),
``doctor`` checks (check), and ``hook.handle_session_start`` (session start).
:class:`ArrivalSummary` is new: no arrival type exists yet, so it is the
smallest frozen dataclass that names what a sink needs.  The signatures are
settled for good only once a second adapter (codex) is built against them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, Self, final

__all__ = [
    "REGISTRY",
    "ArrivalKind",
    "ArrivalSummary",
    "DeliverySink",
    "InstallOutcome",
    "InstallScope",
    "Installer",
    "PluginRegistry",
    "SessionStart",
    "ToolAdapter",
    "ToolCheck",
]


class InstallScope(StrEnum):
    """Where an installer acts: one repository, or the whole machine."""

    REPO = "repo"
    MACHINE = "machine"


class ArrivalKind(StrEnum):
    """What arrived."""

    MESSAGE = "message"
    WALL = "wall"
    TALK = "talk"


@dataclass(frozen=True, slots=True)
class ArrivalSummary:
    """What the engine tells a sink when something arrives for a session."""

    kind: ArrivalKind
    session_key: str
    count: int
    text: str


@dataclass(frozen=True, slots=True)
class InstallOutcome:
    """What an install or uninstall changed, or why it refused.

    Mirrors ``PermissionChange``: ``changed`` names what was written or
    removed; an empty tuple with no ``error`` means already current.
    """

    changed: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True, slots=True)
class ToolCheck:
    """One diagnostic result for a tool, shaped like ``doctor.CheckResult``."""

    name: str
    passed: bool
    message: str
    required: bool = True


class Installer(Protocol):
    """Install, uninstall and check what one tool needs.

    *root* is the repository for :attr:`InstallScope.REPO` and ignored for
    :attr:`InstallScope.MACHINE`.
    """

    def install(self, scope: InstallScope, root: Path) -> InstallOutcome:
        """Write what the tool needs; idempotent."""
        ...

    def uninstall(self, scope: InstallScope, root: Path) -> InstallOutcome:
        """Remove exactly what :meth:`install` wrote; idempotent."""
        ...

    def check(self, scope: InstallScope, root: Path) -> tuple[ToolCheck, ...]:
        """Report whether the tool's files are present and current."""
        ...


class DeliverySink(Protocol):
    """Get an arrival in front of one tool's model."""

    async def deliver(self, summary: ArrivalSummary) -> None:
        """Deliver *summary* to the tool, by whatever channel it supports."""
        ...


class SessionStart(Protocol):
    """Produce the text a session sees when it starts."""

    def context(self, payload: Mapping[str, object]) -> str:
        """Return the start-of-session text for the tool's hook *payload*."""
        ...


@final
@dataclass(frozen=True, slots=True)
class ToolAdapter:
    """One tool's implementations.  A field is ``None`` when the tool needs none."""

    installer: Installer | None = None
    sink: DeliverySink | None = None
    session_start: SessionStart | None = None


@final
class PluginRegistry:
    """Maps a tool name to its adapter."""

    __slots__ = ("_adapters",)

    _adapters: dict[str, ToolAdapter]

    def __new__(cls) -> Self:
        """Create an empty registry."""
        self = super().__new__(cls)
        self._adapters = {}
        return self

    def register(self, name: str, adapter: ToolAdapter) -> None:
        """Add *adapter* under *name*.

        Raises:
            ValueError: *name* is already registered.
        """
        if name in self._adapters:
            msg = f"Plugin already registered: {name}"
            raise ValueError(msg)
        self._adapters[name] = adapter

    def get(self, name: str) -> ToolAdapter:
        """Return the adapter for *name*.

        Raises:
            KeyError: no adapter is registered under *name*.
        """
        try:
            return self._adapters[name]
        except KeyError:
            msg = f"Unknown plugin: {name}"
            raise KeyError(msg) from None

    def names(self) -> tuple[str, ...]:
        """Return the registered tool names in registration order."""
        return tuple(self._adapters)


REGISTRY: Final = PluginRegistry()
