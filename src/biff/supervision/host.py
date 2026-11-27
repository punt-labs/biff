"""Which service manager this host has."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, final

from biff.supervision.launchd import LaunchdBackend
from biff.supervision.systemd import SystemdBackend

if TYPE_CHECKING:
    from biff.supervision.backend import ServiceBackend
    from biff.supervision.spec import ServiceSpec

__all__ = ["Detection", "ServiceHost"]


@dataclass(frozen=True, slots=True)
class Detection:
    """What detection found: a manager, none, or one that does not answer.

    ``backend`` is ``None`` unless the status is ``found``: ``absent`` is a host
    with no user-level manager, as a container is, and ``unreachable`` is a host
    whose manager exists and could not be used, with ``reason`` saying why.
    """

    status: Literal["found", "absent", "unreachable"]
    # None for absent and unreachable: there is no manager to drive.
    backend: ServiceBackend | None
    reason: str


@final
class ServiceHost:
    """Finds the backend for the platform, or says why there is none."""

    @staticmethod
    def detect(spec: ServiceSpec) -> Detection:
        """Return the service manager of this host for *spec*.

        Never raises for a manager that is missing, hung or failing: each is a
        ``Detection`` the caller reports.
        """
        system = platform.system()
        if system == "Darwin":
            return ServiceHost._launchd(spec)
        if system == "Linux":
            return ServiceHost._systemd(spec)
        return Detection("absent", None, f"{system} has no supported service manager")

    @staticmethod
    def _systemd(spec: ServiceSpec) -> Detection:
        reason = SystemdBackend.unavailable_reason()
        if not reason:
            return Detection("found", SystemdBackend(spec), "")
        if reason == "systemctl is not installed":
            return Detection("absent", None, reason)
        return Detection("unreachable", None, reason)

    @staticmethod
    def _launchd(spec: ServiceSpec) -> Detection:
        if LaunchdBackend.available():
            return Detection("found", LaunchdBackend(spec), "")
        return Detection("absent", None, "launchctl is not available")
