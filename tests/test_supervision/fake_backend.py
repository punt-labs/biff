"""A recording stand-in for the platform service backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from biff.supervision.port import PortState


def port_probe(state: PortState) -> Callable[[], PortState]:
    """Return a port probe that always reports *state*."""

    def probe() -> PortState:
        return state

    return probe


@final
class FakeBackend:
    """Satisfies ``ServiceBackend``; records each supervisor call it receives."""

    __slots__ = ("_active", "_calls", "_ok", "_path")

    _path: Path
    _calls: list[str]
    _active: bool
    _ok: bool

    def __new__(cls, config_path: Path, *, ok: bool = True) -> Self:
        """Create a backend whose unit file would live at *config_path*.

        With ``ok=False`` every start, stop and restart reports failure.
        """
        self = super().__new__(cls)
        self._path = config_path
        self._calls = []
        self._active = False
        self._ok = ok
        return self

    @property
    def calls(self) -> list[str]:
        """The supervisor calls received so far, in order."""
        return self._calls

    def enable_autostart(self) -> bool:
        """Record the call."""
        self._calls.append("enable_autostart")
        return self._ok

    def install(self) -> bool:
        """Write a unit file marker and mark the service running."""
        self._calls.append("install")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text("unit")
        self._active = True
        return False

    def uninstall(self) -> None:
        """Remove the unit file marker."""
        self._calls.append("uninstall")
        self._path.unlink(missing_ok=True)
        self._active = False

    def stop(self) -> bool:
        """Mark the service stopped."""
        self._calls.append("stop")
        if self._ok:
            self._active = False
        return self._ok

    def start(self) -> bool:
        """Mark the service running."""
        self._calls.append("start")
        self._active = self._ok
        return self._ok

    def restart(self) -> bool:
        """Mark the service running."""
        self._calls.append("restart")
        self._active = self._ok
        return self._ok

    def is_active(self) -> bool:
        """Return whether the service was started and not stopped."""
        return self._active

    def config_path(self) -> Path:
        """Return where the unit file lives."""
        return self._path

    def linger_warning(self) -> str:
        """Return no warning."""
        return ""
