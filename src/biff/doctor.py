"""Environment diagnostics for biff.

Each check function returns a :class:`CheckResult`.  The
:func:`check_environment` aggregator runs all checks, prints
results, and returns an exit code.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from biff.config import find_git_root, is_enabled, resolve_relay_config
from biff.plugins.contract import CheckResult
from biff.relay_check import LocalRelayCheck

if TYPE_CHECKING:
    from biff.models import RelayAuth
    from biff.plugins.contract import PluginRegistry

PLUGIN_ID = "biff@punt-labs"
COMMANDS_DIR = Path.home() / ".claude" / "commands"
BIFF_COMMANDS = (
    "finger.md",
    "last.md",
    "mesg.md",
    "plan.md",
    "read.md",
    "talk.md",
    "tty.md",
    "wall.md",
    "who.md",
    "write.md",
)


_REACHABILITY = {True: "reachable", False: "unreachable"}

# Individual checks ----------------------------------------------------------


def _check_gh_cli() -> CheckResult:
    """Check ``gh`` CLI is installed and authenticated."""
    gh = shutil.which("gh")
    if not gh:
        return CheckResult(
            "gh CLI",
            False,
            "not found (install: brew install gh)",
            required=False,
        )
    result = subprocess.run(
        [gh, "auth", "status"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return CheckResult(
            "gh CLI", False, "not authenticated (run: gh auth login)", required=False
        )
    return CheckResult("gh CLI", True, "authenticated", required=False)


def _registry_path() -> Path:
    return Path.home() / ".claude" / "plugins" / "installed_plugins.json"


def _plugin_registry_entries() -> list[object] | None:
    """Return the biff plugin's registry entries, or ``None`` if unreadable.

    ``None`` means the registry could not be read as a plugin registry at all:
    a read/parse error, or a shape deviation (top-level not an object,
    ``plugins`` not an object, the plugin value present but not a list). A
    *well-formed* registry that simply has no biff entry returns ``[]`` — that
    is "not installed", a distinct outcome from "could not read". Never raises;
    the ``--version`` flag reads this and must never crash.
    """
    try:
        registry: object = json.loads(_registry_path().read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(registry, dict):
        return None
    registry_map = cast("dict[str, object]", registry)
    plugins = registry_map.get("plugins")
    if not isinstance(plugins, dict):
        return None
    plugins_map = cast("dict[str, object]", plugins)
    entries = plugins_map.get(PLUGIN_ID)
    if entries is None:
        return []  # well-formed registry, biff simply not installed
    if not isinstance(entries, list):
        return None
    return cast("list[object]", entries)


def installed_plugin_version() -> str | None:
    """Return the installed biff plugin version, or ``None`` if unresolvable.

    ``None`` is the contract for "no plugin install found": a CLI-only
    install, a missing, unreadable, or malformed registry, or an entry
    without a version.
    """
    entries = _plugin_registry_entries()
    if entries is None:
        return None
    versions: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        version = cast("dict[str, object]", entry).get("version")
        if isinstance(version, str):
            versions.append(version)
    return versions[-1] if versions else None


def _check_plugin_installed() -> CheckResult:
    """Check biff plugin is installed via marketplace."""
    if not _registry_path().exists():
        return CheckResult("Plugin", False, "not installed (run: biff install)")
    entries = _plugin_registry_entries()
    if entries is None:
        return CheckResult("Plugin", False, "could not read plugin registry")
    if entries:
        return CheckResult("Plugin", True, f"{PLUGIN_ID} installed")
    return CheckResult("Plugin", False, "not installed (run: biff install)")


def _check_user_commands(commands_dir: Path | None = None) -> CheckResult:
    """Check top-level user commands are deployed (informational)."""
    target = commands_dir or COMMANDS_DIR
    missing = sorted(name for name in BIFF_COMMANDS if not (target / name).exists())
    if not missing:
        return CheckResult(
            "User commands",
            True,
            f"{len(BIFF_COMMANDS)} commands in {target}",
            required=False,
        )
    return CheckResult(
        "User commands",
        False,
        f"missing: {', '.join(missing)} (restart Claude Code to deploy)",
        required=False,
    )


def _resolve_relay_config() -> tuple[str | None, RelayAuth | None, bool]:
    """Resolve relay settings without requiring user identity.

    Delegate to the CLI's file and environment resolution chain so doctor
    reports the relay commands actually connect to.

    Raises:
        SystemExit: Shared YAML is malformed or relay overrides are invalid.
    """
    return resolve_relay_config(find_git_root())


async def _test_nats_connection(
    url: str, auth: RelayAuth | None, *, tls_handshake_first: bool
) -> bool:
    """Attempt a NATS connection with a short timeout.

    Retries once after 500ms on failure to handle cold DNS cache
    (first run after install).  Suppresses nats.py ERROR log on
    the first attempt so a transient DNS timeout doesn't produce
    a scary traceback.

    Never raises a redacted ``RelayConnectError`` here -- the raw
    ``nats.connect()`` failure is caught, its occurrence noted for the
    retry loop, and discarded; no exception object carrying the URL or
    auth kwargs ever needs to escape this function's boundary, so there is
    nothing to redact.
    """
    import logging

    import nats

    from biff.nats_relay import safe_close

    tls_kwargs: dict[str, bool] = (
        {"tls_handshake_first": True} if tls_handshake_first else {}
    )
    nats_logger = logging.getLogger("nats")
    saved_level = nats_logger.level

    for attempt in range(2):
        # Suppress nats.py ERROR on first attempt — transient DNS
        # timeout produces a traceback that looks like a real failure.
        if attempt == 0:
            nats_logger.setLevel(logging.CRITICAL)
        # Rebuilt fresh each iteration -- clearing it after attempt 0's
        # failure must not starve the retry of its auth kwargs.
        auth_kwargs: dict[str, str] = dict(auth.as_nats_kwargs()) if auth else {}
        connected = False
        try:
            try:
                nc = await nats.connect(  # pyright: ignore[reportUnknownMemberType]
                    url,
                    connect_timeout=3,
                    **auth_kwargs,
                    **tls_kwargs,
                )
            finally:
                auth_kwargs.clear()
        except Exception:  # noqa: BLE001 — retry once, then report unreachable
            if attempt == 0:
                await asyncio.sleep(0.5)
        else:
            connected = True
            await safe_close(nc)
        finally:
            if attempt == 0:
                nats_logger.setLevel(saved_level)
        if connected:
            return True
    return False


def _check_relay() -> CheckResult:
    """Check the relay: the local one needs no network, a NATS one must connect."""
    # Lazy import -- keep doctor's module-level imports free of nats_relay
    # (and its eager `import nats`) for checks that don't need it.
    from biff.nats_connection import Scrub

    relay_url, relay_auth, tls = _resolve_relay_config()
    if relay_url is None:
        return LocalRelayCheck.of(find_git_root())
    # A NATS URL may embed user:pass@ -- doctor's success/failure messages
    # go straight to the operator's terminal, so they must never echo the
    # raw credential-bearing URL.
    host = Scrub.relay_url(relay_url)

    try:
        reachable = asyncio.run(
            _test_nats_connection(relay_url, relay_auth, tls_handshake_first=tls)
        )
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {str(exc).replace(relay_url, host)}"
        return CheckResult("NATS relay", False, f"connection error ({host}): {detail}")

    return CheckResult("NATS relay", reachable, f"{_REACHABILITY[reachable]} ({host})")


def _check_biff_file() -> CheckResult:
    """Check config.yaml or zero-config is available (informational)."""
    repo_root = find_git_root()
    if repo_root is None:
        return CheckResult(
            "Config",
            False,
            "not in a git repo (run 'biff enable' inside a project)",
            required=False,
        )
    config_yaml = repo_root / ".punt-labs" / "biff" / "config.yaml"
    if config_yaml.exists():
        return CheckResult("Config", True, str(config_yaml), required=False)
    return CheckResult(
        "Config",
        True,
        "zero-config (defaults from git remote)",
        required=False,
    )


def _check_enabled() -> CheckResult:
    """Check whether the committed ``.punt-labs/biff/enabled`` marker exists.

    Informational.
    """
    repo_root = find_git_root()
    if repo_root is None:
        return CheckResult("Enabled", False, "not in a git repo", required=False)
    if is_enabled(repo_root):
        return CheckResult(
            "Enabled", True, "yes (.punt-labs/biff/enabled)", required=False
        )
    return CheckResult(
        "Enabled",
        False,
        "no (run 'biff enable' to activate)",
        required=False,
    )


def _check_git_hooks() -> CheckResult:
    """Check biff git hooks are deployed (informational)."""
    repo_root = find_git_root()
    if repo_root is None:
        return CheckResult("Git hooks", False, "not in a git repo", required=False)
    if not is_enabled(repo_root):
        return CheckResult(
            "Git hooks",
            True,
            "skipped (biff not enabled)",
            required=False,
        )
    from biff.git_hooks import check_git_hooks

    missing = check_git_hooks(repo_root)
    if not missing:
        return CheckResult("Git hooks", True, "all deployed", required=False)
    return CheckResult(
        "Git hooks",
        False,
        f"missing: {', '.join(missing)} (run 'biff enable')",
        required=False,
    )


def _check_ci_workflow() -> CheckResult:
    """Check biff CI notification workflow is deployed (informational)."""
    repo_root = find_git_root()
    if repo_root is None:
        return CheckResult(
            "CI workflow",
            False,
            "not in a git repo",
            required=False,
        )
    if not is_enabled(repo_root):
        return CheckResult(
            "CI workflow",
            True,
            "skipped (biff not enabled)",
            required=False,
        )
    from biff.ci_workflow import check_ci_workflow

    if check_ci_workflow(repo_root):
        return LocalRelayCheck.ci_workflow(repo_root)
    return CheckResult(
        "CI workflow",
        False,
        "missing or outdated (run 'biff enable')",
        required=False,
    )


# Aggregator -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Diagnostics:
    """Run every diagnostic and report them: biff's, then each registered tool's."""

    _plugins: PluginRegistry

    def run(self) -> int:
        """Print all diagnostics.  Returns 0 if all required pass, 1 otherwise."""
        from importlib.metadata import version

        print(f"punt-biff {version('punt-biff')}")
        print()

        checks = self._checks()
        for check in checks:
            self._print(check)

        required_failures = [c for c in checks if c.required and not c.passed]
        if required_failures:
            count = len(required_failures)
            print(f"\n{count} required check(s) failed.")
            return 1
        print("\nAll required checks passed.")
        return 0

    def _checks(self) -> list[CheckResult]:
        return [
            _check_gh_cli(),
            _check_plugin_installed(),
            _check_user_commands(),
            _check_relay(),
            _check_biff_file(),
            _check_enabled(),
            _check_git_hooks(),
            _check_ci_workflow(),
            *(c for i in self._plugins.installers() for c in i.check()),
        ]

    @staticmethod
    def _print(check: CheckResult) -> None:
        """Print a single check result with the appropriate symbol."""
        if check.passed:
            symbol = "\u2713"  # ✓
        elif check.required:
            symbol = "\u2717"  # ✗
        else:
            symbol = "\u25cb"  # ○
        print(f"  {symbol} {check.name}: {check.message}")
