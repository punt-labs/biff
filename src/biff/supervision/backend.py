"""The platform-neutral interface of a service manager."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["ServiceBackend"]


class ServiceBackend(Protocol):
    """What a launchd or systemd backend does for one service.

    The methods are those of ``punt_service``'s backend, so a backend from that
    library satisfies this protocol as it is.
    """

    def enable_autostart(self) -> bool:
        """Enable login startup without changing the running service."""
        ...

    def install(self) -> bool:
        """Register and start the service; return whether it was a no-op."""
        ...

    def uninstall(self) -> None:
        """Stop and remove the service."""
        ...

    def stop(self) -> bool:
        """Stop the service, leaving it registered; return whether it worked."""
        ...

    def start(self) -> bool:
        """Start the registered service; return whether it worked."""
        ...

    def restart(self) -> bool:
        """Restart the service under its supervisor; return whether it worked."""
        ...

    def is_active(self) -> bool:
        """Return whether the service is running."""
        ...

    def config_path(self) -> Path:
        """Return the path of the unit file or plist."""
        ...

    def linger_warning(self) -> str:
        """Return a warning line to print after install, or an empty string."""
        ...
