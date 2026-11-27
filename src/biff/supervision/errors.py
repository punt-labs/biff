"""The failures a service manager reports."""

from __future__ import annotations

__all__ = [
    "BinaryNotFoundError",
    "PortConflictError",
    "ServiceActionFailedError",
    "ServiceNotInstalledError",
]


class BinaryNotFoundError(RuntimeError):
    """The service's executable is not at its stable install path."""


class PortConflictError(RuntimeError):
    """Another process holds the port the service needs."""


class ServiceActionFailedError(RuntimeError):
    """The supervisor refused a start, stop or restart."""


class ServiceNotInstalledError(RuntimeError):
    """The service has no unit or plist to act on."""
