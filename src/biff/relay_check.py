"""Check that the local relay's directory can be written, for ``biff doctor``."""

from __future__ import annotations

import os
from pathlib import Path

from biff.plugins.contract import CheckResult
from biff.relay_config import RelayLocation


class LocalRelayCheck:
    """The doctor check for a repo's local relay directory."""

    @staticmethod
    def of(repo_root: Path | None) -> CheckResult:
        """Return a check naming the directory, why it is that one, and any error."""
        if repo_root is None:
            return CheckResult(
                "Relay", passed=True, message="local filesystem relay (not in a repo)"
            )
        location = RelayLocation.of(repo_root)
        error = LocalRelayCheck._probe(location.path)
        if error is not None:
            return CheckResult(
                "Relay",
                passed=False,
                message=(
                    f"local relay directory {location.path} is not writable: "
                    f"{error}; fix its permissions, or choose another relay in config"
                ),
            )
        return CheckResult(
            "Relay",
            passed=True,
            message=f"local filesystem relay {location.path} ({location.reason})",
        )

    @staticmethod
    def _probe(path: Path) -> OSError | None:
        """Create *path*, then create, write and remove a file in it."""
        probe = path / f".probe-{os.getpid()}"
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            probe.write_text("ok")
            probe.unlink()
        except OSError as exc:
            return exc
        return None
