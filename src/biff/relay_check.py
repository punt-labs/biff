"""Check that the local relay's directory can be written, for ``biff doctor``."""

from __future__ import annotations

import os
from pathlib import Path

from biff.config import load_yaml_config
from biff.plugins.contract import CheckResult
from biff.relay_config import RelayFile, RelayLocation


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
    def ci_workflow(repo_root: Path) -> CheckResult:
        """Return the doctor check for a deployed, current CI notify workflow."""
        warning = LocalRelayCheck.notify_warning(repo_root)
        return CheckResult(
            "CI workflow",
            passed=warning is None,
            message=warning or "biff-notify.yml deployed",
            required=False,
        )

    @staticmethod
    def enable_note(repo_root: Path) -> str:
        """Return the warning ``biff enable`` prints, as a line, or nothing."""
        warning = LocalRelayCheck.notify_warning(repo_root)
        return "" if warning is None else f"Warning: {warning}\n"

    @staticmethod
    def notify_warning(repo_root: Path) -> str | None:
        """Return why an installed CI notify workflow cannot deliver, or ``None``.

        It cannot when the repo resolves to the local relay: a CI runner is
        another machine, so the notification would reach no one.
        """
        workflow = repo_root / ".github" / "workflows" / "biff-notify.yml"
        committed = RelayFile.parse("config.yaml", load_yaml_config(repo_root))
        if committed.chooses() and committed.kind() != "local":
            return None  # CI reads the committed relay, nothing of yours
        if not workflow.is_file():
            return None
        return (
            "biff-notify.yml is installed but the committed config chooses no "
            "network relay, so CI would use the local relay and reach no one "
            "unless a repository variable supplies one: commit a demo or nats "
            "relay in .punt-labs/biff/config.yaml, or set BIFF_RELAY_URL "
            "(vars.BIFF_RELAY_URL)"
            + (
                " and relay.allow_env_override: true (a committed relay ignores "
                "the variable otherwise)"
                if committed.declares()
                else ""
            )
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
