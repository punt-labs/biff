"""Which service manager this host has."""

from __future__ import annotations

import platform
from typing import TYPE_CHECKING, final

from biff.supervision.launchd import LaunchdBackend
from biff.supervision.systemd import SystemdBackend

if TYPE_CHECKING:
    from biff.supervision.backend import ServiceBackend
    from biff.supervision.spec import ServiceSpec

__all__ = ["ServiceHost"]


@final
class ServiceHost:
    """Finds the backend for the platform, or says there is none."""

    @staticmethod
    def backend(spec: ServiceSpec) -> ServiceBackend | None:
        """Return the backend for *spec* on this host.

        ``None`` means the host has no user-level service manager: a container,
        or a Linux login without a systemd user session.  That absence is a
        normal state, which callers answer by running biffd detached.
        """
        system = platform.system()
        if system == "Darwin" and LaunchdBackend.available():
            return LaunchdBackend(spec)
        if system == "Linux" and SystemdBackend.available():
            return SystemdBackend(spec)
        return None
