"""The plugin contract: what an AI tool's adapter implements for install and doctor.

Biff is the same for every AI coding tool; only what must be installed for a
tool differs.  :class:`Installer` names those differences, and
:class:`PluginRegistry` holds the installers.  ``cli`` builds the registry at
startup and hands it to ``enable``, ``disable`` and ``doctor``; nothing below
``cli`` imports an adapter (DES-088).

The methods are the ones ``RepoEnablement`` and ``doctor`` called on Claude
Code's modules before the adapter existed.  :class:`PermissionReport` is the
shape of that module's own ``PermissionChange``, stated structurally so the
adapter needs no import from here, and :class:`CheckResult` is ``doctor``'s
own result type, moved down so installers can return it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Self, final

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

__all__ = ["CheckResult", "Installer", "PermissionReport", "PluginRegistry"]


class PermissionReport(Protocol):
    """What a grant or revoke did to a tool's permission file in a repo."""

    @property
    def changed(self) -> tuple[str, ...]:
        """The rules added (grant) or removed (revoke)."""
        ...

    @property
    def error(self) -> str:
        """Why the file was left untouched; empty when nothing went wrong."""
        ...

    @property
    def fix(self) -> str:
        """What the operator does about the refusal in :attr:`error`."""
        ...

    def lines(self, verb: str) -> list[str]:
        """Return one report line per changed rule, the refusal, or none."""
        ...


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Outcome of a single diagnostic check."""

    name: str
    passed: bool
    message: str
    required: bool = True


class Installer(Protocol):
    """Install, uninstall and check what one AI tool needs from a repo."""

    def preflight(self, root: Path) -> PermissionReport:
        """Return an ``error`` when install would be refused; write nothing."""
        ...

    def grant(self, root: Path) -> PermissionReport:
        """Allow biff's tools and commands in the tool's permission file."""
        ...

    def revoke(
        self, root: Path, rules: Iterable[str] | None = None
    ) -> PermissionReport:
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

    def check(self, user_override: str | None) -> Iterable[CheckResult]:
        """Run the tool's diagnostics for ``biff doctor``.

        *user_override* is ``--user``.  Results are produced lazily, so a slow
        one does not hold back the lines before it.
        """
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
