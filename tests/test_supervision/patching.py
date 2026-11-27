"""Typed patches of what the host's service manager and port look like."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.supervision.host import Detection, ServiceHost
from biff.supervision.port import PortProbe

if TYPE_CHECKING:
    import pytest

    from biff.supervision.backend import ServiceBackend
    from biff.supervision.port import PortState
    from biff.supervision.spec import ServiceSpec


def host_is(monkeypatch: pytest.MonkeyPatch, found: Detection) -> None:
    """Make ``ServiceHost.detect`` answer *found*."""

    def detect(spec: ServiceSpec) -> Detection:
        return found

    monkeypatch.setattr(ServiceHost, "detect", staticmethod(detect))


def host_has(monkeypatch: pytest.MonkeyPatch, backend: ServiceBackend | None) -> None:
    """Make the host's manager *backend*; ``None`` is no manager at all."""
    if backend is None:
        host_is(monkeypatch, Detection("absent", None, ""))
    else:
        host_is(monkeypatch, Detection("found", backend, ""))


def host_unreachable(monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    """Make the host have a manager that does not answer, for *reason*."""
    host_is(monkeypatch, Detection("unreachable", None, reason))


def port_is(monkeypatch: pytest.MonkeyPatch, state: PortState) -> None:
    """Make every ``PortProbe`` report *state*, whatever the real port holds."""

    def report(self: PortProbe) -> PortState:
        return state

    monkeypatch.setattr(PortProbe, "state", report)


def port_answers(monkeypatch: pytest.MonkeyPatch, *, answers: bool) -> None:
    """Make every ``PortProbe`` say biffd does or does not answer ``/health``."""

    def wait(self: PortProbe, seconds: float) -> bool:
        return answers

    monkeypatch.setattr(PortProbe, "wait_until_answering", wait)


def no_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``shutil.which`` find nothing, so ``biff install`` skips the plugin."""

    def find(name: str) -> None:
        return None

    monkeypatch.setattr("shutil.which", find)
