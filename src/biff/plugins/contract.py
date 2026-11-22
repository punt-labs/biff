"""The plugin contract: what an AI tool's adapter implements for install and doctor.

Biff is the same for every AI coding tool; only what must be installed for a
tool differs.  :class:`Installer` names those differences, and
:class:`PluginRegistry` holds the installers.  ``cli`` builds the registry at
startup and hands it to ``enable``, ``disable``, ``doctor`` and the MCP
server; nothing below ``cli`` imports an adapter (DES-088).

The methods are the ones ``RepoEnablement`` and ``doctor`` called on Claude
Code's modules before the adapter existed.  :class:`PermissionChange` and
:class:`CheckResult` are those modules' own result types, moved here so the
contract and the service code share them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Self, final

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

__all__ = ["CheckResult", "Installer", "PermissionChange", "PluginRegistry"]


@dataclass(frozen=True, slots=True)
class PermissionChange:
    """What a grant or revoke did to a tool's permission file in a repo.

    ``changed`` lists the rules added (grant) or removed (revoke); an empty
    tuple with no ``error`` means the file already matched.  ``error`` explains
    why the file was left untouched, and ``fix`` says what the operator does
    about it.  ``where`` names the file for report lines.
    """

    changed: tuple[str, ...] = ()
    error: str = ""
    where: str = ""
    fix: str = ""

    def lines(self, verb: str) -> list[str]:
        """Return one report line per changed rule, the refusal, or none."""
        if self.error:
            return [f"Permissions not changed: {self.error}"]
        return [f"Permission {verb} {self.where}: {r}" for r in self.changed]


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Outcome of a single diagnostic check."""

    name: str
    passed: bool
    message: str
    required: bool = True


class Installer(Protocol):
    """Install, uninstall and check what one AI tool needs from a repo."""

    def preflight(self, root: Path) -> PermissionChange:
        """Return an ``error`` when install would be refused; write nothing."""
        ...

    def grant(self, root: Path) -> PermissionChange:
        """Allow biff's tools and commands in the tool's permission file."""
        ...

    def revoke(
        self, root: Path, rules: Iterable[str] | None = None
    ) -> PermissionChange:
        """Remove *rules* (default: every rule biff owns) from that file."""
        ...

    def register_guide(self, root: Path) -> bool:
        """Import biff's agent guide in the tool's instruction file.

        Returns ``True`` when the file changed.
        """
        ...

    def unregister_guide(self, root: Path) -> bool:
        """Remove that import.  Returns ``True`` when the file changed."""
        ...

    def check(self) -> tuple[CheckResult, ...]:
        """Run the tool's diagnostics for ``biff doctor``."""
        ...


@final
class PluginRegistry:
    """The installers ``cli`` registered, in registration order."""

    __slots__ = ("_installers",)

    _installers: dict[str, Installer]

    def __new__(cls) -> Self:
        """Create an empty registry."""
        self = super().__new__(cls)
        self._installers = {}
        return self

    def register(self, name: str, installer: Installer) -> None:
        """Add *installer* for the tool called *name*.

        Raises:
            ValueError: *name* is already registered.
        """
        if name in self._installers:
            msg = f"Plugin already registered: {name}"
            raise ValueError(msg)
        self._installers[name] = installer

    def installers(self) -> tuple[Installer, ...]:
        """Return every registered installer."""
        return tuple(self._installers.values())
