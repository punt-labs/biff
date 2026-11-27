"""Typed patches of what the host's service manager and port look like."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.supervision.host import ServiceHost
from biff.supervision.port import PortProbe

if TYPE_CHECKING:
    import pytest

    from biff.supervision.backend import ServiceBackend
    from biff.supervision.port import PortState
    from biff.supervision.spec import ServiceSpec


def host_has(monkeypatch: pytest.MonkeyPatch, backend: ServiceBackend | None) -> None:
    """Make ``ServiceHost.backend`` answer *backend*; ``None`` is no manager."""

    def find(spec: ServiceSpec) -> ServiceBackend | None:
        return backend

    monkeypatch.setattr(ServiceHost, "backend", staticmethod(find))


def port_is(monkeypatch: pytest.MonkeyPatch, state: PortState) -> None:
    """Make every ``PortProbe`` report *state*, whatever the real port holds."""

    def report(self: PortProbe) -> PortState:
        return state

    monkeypatch.setattr(PortProbe, "state", report)


def no_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``shutil.which`` find nothing, so ``biff install`` skips the plugin."""

    def find(name: str) -> None:
        return None

    monkeypatch.setattr("shutil.which", find)
